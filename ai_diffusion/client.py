from enum import Enum
from collections import deque
import json
import struct
import uuid
from typing import NamedTuple, Optional, Union, Sequence

from .comfyworkflow import ComfyWorkflow
from .image import Image, ImageCollection
from .network import RequestManager, NetworkError
from .websockets.src import websockets


class ClientEvent(Enum):
    progress = 0
    finished = 1
    interrupted = 2


class ClientMessage(NamedTuple):
    event: ClientEvent
    job_id: str
    progress: float
    images: ImageCollection = None


class JobInfo(NamedTuple):
    id: str
    node_count: int
    sample_count: int


class Progress:
    _nodes = 0
    _samples = 0
    _info: JobInfo

    def __init__(self, job_info: JobInfo):
        self._info = job_info

    def handle(self, msg: dict):
        id = msg["data"].get("prompt_id", None)
        if id is not None and id != self._info.id:
            return
        if msg["type"] == "executing":
            self._nodes += 1
        elif msg["type"] == "execution_cached":
            self._nodes += len(msg["data"]["nodes"])
        elif msg["type"] == "progress":
            self._samples += 1

    @property
    def value(self):
        # Add +1 to node count so progress doesn't go to 100% until images are received.
        node_part = self._nodes / (self._info.node_count + 1)
        sample_part = self._samples / max(self._info.sample_count, 1)
        return 0.2 * node_part + 0.8 * sample_part


class ResourceKind(Enum):
    checkpoint = "SD Checkpoint"
    controlnet = "ControlNet model"
    clip_vision = "CLIP Vision model"
    ip_adapter = "IP-Adapter model"
    node = "custom node"


class MissingResource(Exception):
    kind: ResourceKind
    names: Optional[Sequence[str]]

    def __init__(self, kind: ResourceKind, names: Optional[Sequence[str]] = None):
        self.kind = kind
        self.names = names

    def __str__(self):
        return f"Missing {self.kind.value}: {', '.join(self.names)}"


class Client:
    """HTTP/WebSocket client which sends requests to and listens to messages from a ComfyUI server."""

    default_url = "127.0.0.1:8188"

    _requests = RequestManager()
    _websocket: websockets.WebSocketClientProtocol
    _id: str
    _jobs: deque
    _active: Optional[JobInfo] = None

    url: str
    checkpoints: Sequence[str]
    lora_models: Sequence[str]
    controlnet_model: dict
    clip_vision_model: str
    ip_adapter_model: str

    @staticmethod
    async def connect(url=default_url):
        client = Client(url)
        try:
            client._websocket = await websockets.connect(
                f"ws://{url}/ws?clientId={client._id}", max_size=2**30, read_limit=2**30
            )
        except OSError as e:
            raise NetworkError(
                e.errno, f"Could not connect to websocket server at {url}: {str(e)}", url
            )
        # Check custom nodes
        nodes = await client._get("object_info")
        missing = [
            package
            for package, package_nodes in ComfyWorkflow.required_custom_nodes.items()
            if any(node not in nodes for node in package_nodes)
        ]
        if len(missing) > 0:
            raise MissingResource(ResourceKind.node, missing)

        # Retrieve SD checkpoints
        client.checkpoints = nodes["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        if len(client.checkpoints) == 0:
            raise MissingResource(ResourceKind.checkpoint)

        # Retrieve LoRA models
        client.lora_models = nodes["LoraLoader"]["input"]["required"]["lora_name"][0]

        # Retrieve ControlNet models
        cns = nodes["ControlNetLoader"]["input"]["required"]["control_net_name"][0]
        client.controlnet_model = {
            "inpaint": _find_controlnet_model(cns, "control_v11p_sd15_inpaint")
        }

        # Retrieve CLIPVision models
        cv = nodes["CLIPVisionLoader"]["input"]["required"]["clip_name"][0]
        client.clip_vision_model = _find_clip_vision_model(cv, "SD1.5")

        # Retrieve IP-Adapter model
        ip = nodes["IPAdapter"]["input"]["required"]["model_name"][0]
        client.ip_adapter_model = _find_ip_adapter(ip, "sd15")

        return client

    def __init__(self, url):
        self.url = url
        self._id = str(uuid.uuid4())
        self._jobs = deque()

    async def _get(self, op: str):
        return await self._requests.get(f"{self.url}/{op}")

    async def _post(self, op: str, data: dict):
        return await self._requests.post(f"{self.url}/{op}", data)

    async def enqueue(self, workflow: ComfyWorkflow):
        data = {"prompt": workflow.root, "client_id": self._id}
        result = await self._post("prompt", data)
        job_id = result["prompt_id"]
        self._jobs.append(JobInfo(job_id, workflow.node_count, workflow.sample_count))
        return job_id

    async def listen(self):
        progress = None
        images = ImageCollection()

        async for msg in self._websocket:
            if isinstance(msg, bytes):
                image = _extract_message_png_image(msg)
                if image is not None:
                    images.append(image)

            elif isinstance(msg, str):
                msg = json.loads(msg)
                if msg["type"] == "execution_start":
                    id = msg["data"]["prompt_id"]
                    self._active = self._start_job(id)
                    progress = Progress(self._active)
                    images = ImageCollection()

                if msg["type"] == "execution_interrupted":
                    job = self._get_active_job(msg["data"]["prompt_id"])
                    if job:
                        self._clear_job(job.id)
                        yield ClientMessage(ClientEvent.interrupted, job.id, 0)

                if msg["type"] == "executing" and msg["data"]["node"] is None:
                    self._clear_job(msg["data"]["prompt_id"])

                elif msg["type"] in ("execution_cached", "executing", "progress"):
                    progress.handle(msg)
                    yield ClientMessage(ClientEvent.progress, self._active.id, progress.value)

                if msg["type"] == "executed":
                    job = self._get_active_job(msg["data"]["prompt_id"])
                    if job and _validate_executed_node(msg, len(images)):
                        self._clear_job(job.id)
                        yield ClientMessage(ClientEvent.finished, job.id, 1, images)

    async def interrupt(self):
        await self._post("interrupt", {})

    @property
    def queued_count(self):
        return len(self._jobs)

    @property
    def is_executing(self):
        return self._active is not None

    def _get_active_job(self, id: str) -> Optional[JobInfo]:
        if self._active and self._active.id == id:
            return self._active
        else:
            print(
                f"[krita-ai-diffusion] received message for job {id}, but"
                f" job {self._active.id} is active"
            )
        if len(self._jobs) == 0:
            print(f"[krita-ai-diffusion] received unknown job {id}")
            return None
        active = next((j for j in self._jobs if j.id == id), None)
        if active is not None:
            return active
        return None

    def _start_job(self, id: str):
        if self._active is not None:
            print(
                f"[krita-ai-diffusion] started job {id}, but {self._active.id} was never finished"
            )
        if len(self._jobs) == 0:
            print(f"[krita-ai-diffusion] received unknown job {id}")
            return None
        if self._jobs[0].id == id:
            return self._jobs.popleft()
        print(f"[krita-ai-diffusion] started job {id}, but {self._jobs[0].id} was expected")
        active = next((j for j in self._jobs if j.id == id), None)
        if active is not None:
            self._jobs.remove(active)
            return active
        return None

    def _clear_job(self, job_id: str):
        if self._active is not None and self._active.id == job_id:
            self._active = None


def _find_controlnet_model(model_list: Sequence[str], model_name: str):
    model = next((model for model in model_list if model.startswith(model_name)), None)
    if model is None:
        raise MissingResource(ResourceKind.controlnet, [model_name])
    return model


def _find_clip_vision_model(model_list: Sequence[str], sdver: str):
    model_name = "clip_vision_g.safetensors" if sdver == "SDXL" else "pytorch_model.bin"
    match = lambda x: (sdver == "SDXL" or sdver in x) and model_name in x
    model = next((m for m in model_list if match(m)), None)
    if model is None:
        full_name = model_name if sdver == "SDXL" else f"{sdver}/{model_name}"
        raise MissingResource(ResourceKind.clip_vision, [full_name])
    return model


def _find_ip_adapter(model_list: Sequence[str], sdver: str):
    model_name = f"ip-adapter_{sdver}"
    model = next((m for m in model_list if model_name in m), None)
    if model is None:
        raise MissingResource(ResourceKind.ip_adapter, [model_name])
    return model


def _extract_message_png_image(data: memoryview):
    s = struct.calcsize(">II")
    if len(data) > s:
        event, format = struct.unpack_from(">II", data)
        # ComfyUI server.py: BinaryEventTypes.PREVIEW_IMAGE=1, PNG=2
        if event == 1 and format == 2:
            return Image.png_from_bytes(data[s:])
    return None


def _validate_executed_node(msg: dict, image_count: int):
    try:
        data = msg["data"]
        output = data["output"]["images"]
        if len(output) != image_count:  # not critical
            print(
                "[krita-ai-diffusion] received number of images does not match:"
                f" {len(output)} != {image_count}"
            )
        if len(output) > 0 and "source" in output[0] and output[0]["type"] == "output":
            return True
    except:
        print("[krita-ai-diffusion] received unknown message format", msg)
        return False
