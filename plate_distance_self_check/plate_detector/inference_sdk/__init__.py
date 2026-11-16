import base64
import os
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
import requests


@dataclass
class InferenceConfiguration:
    confidence_threshold: float | None = None
    iou_threshold: float | None = None
    api_key_transport: str | None = None


class InferenceHTTPClient:
    def __init__(self, api_url: str, api_key: str | None = None):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.config = InferenceConfiguration()

    def configure(self, configuration: InferenceConfiguration):
        self.config = configuration

    def infer(self, image, model_id: str):
        payload = {
            "id": str(uuid.uuid4()),
            "api_key": self.api_key,
            "model_id": model_id,
            "image": {
                "type": "base64",
                "value": _image_to_base64(image),
            },
        }

        if self.config.confidence_threshold is not None:
            payload["confidence"] = self.config.confidence_threshold

        if self.config.iou_threshold is not None:
            payload["iou_threshold"] = self.config.iou_threshold

        endpoint = _endpoint_for_model(model_id)
        response = requests.post(
            f"{self.api_url}{endpoint}",
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        return response.json()


def _endpoint_for_model(model_id: str) -> str:
    lowered = model_id.lower()
    # The deployed YOLO26 instance-segmentation model ID does not contain the
    # literal word "seg", even though it returns polygon masks.  Keep it on
    # the instance-segmentation endpoint instead of misrouting it to object
    # detection and receiving a 404 from the local inference server.
    if "seg" in lowered or "yolo26n" in lowered:
        return "/infer/instance_segmentation"
    return "/infer/object_detection"


def _image_to_base64(image) -> str:
    if isinstance(image, (str, os.PathLike)):
        with open(image, "rb") as file:
            return base64.b64encode(file.read()).decode("ascii")

    if isinstance(image, np.ndarray):
        ok, encoded = cv2.imencode(".jpg", image)
        if not ok:
            raise ValueError("Could not encode numpy image for inference.")
        return base64.b64encode(encoded.tobytes()).decode("ascii")

    raise TypeError(f"Unsupported image input: {type(image)!r}")
