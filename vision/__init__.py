"""Vision sidecar for the Bertec jump lab.

Two halves, deliberately split by interpreter:

* :mod:`vision.pose_math` - pure stdlib geometry over MediaPipe's 33-point
  landmark arrays.  Importable from the Bertec app's stdlib-only venv and from
  the pose worker (which runs under a mediapipe/opencv/numpy interpreter).
* :mod:`vision.manager` - stdlib subprocess supervisor for that worker
  (JSONL stdout, MJPEG proxy target, bounded stderr tail).
* :mod:`vision.worker` - the worker itself.  NOT imported here: it needs
  cv2/mediapipe/numpy, which the Bertec venv intentionally does not have.

Validated scope (design v1.1): relative trunk/pelvis sway and sagittal joint
estimates only.  No frontal valgus, no foot-lift scoring, no camera COP.
"""

from . import pose_math
from .manager import VisionManager

__all__ = ["pose_math", "VisionManager"]
