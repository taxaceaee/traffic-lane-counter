from pathlib import Path

import cv2


class ImageSequenceReader:
    """Reads a sequence of image files from a directory in alphabetical order.

    Provides an interface similar to cv2.VideoCapture.
    """
    def __init__(self, directory_path: str | Path, extensions: list[str] | None = None, fps: float = 25.0):
        self.dir_path = Path(directory_path)
        if not self.dir_path.exists():
            raise FileNotFoundError(f"Directory not found: {self.dir_path}")
        if not self.dir_path.is_dir():
            raise ValueError(f"Path is not a directory: {self.dir_path}")

        if extensions is None:
            extensions = [".jpg", ".jpeg", ".png"]
        # Convert extensions to lower case
        extensions = [ext.lower() for ext in extensions]

        # Find images
        self.image_paths = []
        for file in self.dir_path.iterdir():
            if file.is_file() and file.suffix.lower() in extensions:
                self.image_paths.append(file)

        # Sort alphabetically by filename
        self.image_paths.sort(key=lambda p: p.name)

        self.frame_count = len(self.image_paths)
        self.fps = fps
        self.current_idx = 0
        self._first_frame = None

        # Read the first image to get dimensions
        if self.frame_count > 0:
            first_img = cv2.imread(str(self.image_paths[0]))
            if first_img is None:
                raise ValueError(f"Could not read first image: {self.image_paths[0]}")
            self.height, self.width = first_img.shape[:2]
            self._first_frame = first_img
        else:
            self.height, self.width = 0, 0

    def read(self) -> tuple[bool, cv2.Mat | None]:
        """Reads the next image frame.

        Returns:
            A tuple of (success_flag, image_frame).
        """
        if self.current_idx >= self.frame_count:
            return False, None
        if self.current_idx == 0 and self._first_frame is not None:
            frame = self._first_frame
            self._first_frame = None
        else:
            img_path = self.image_paths[self.current_idx]
            frame = cv2.imread(str(img_path))
            if frame is None:
                return False, None
        self.current_idx += 1
        return True, frame

    def release(self):
        """Mock release to match cv2.VideoCapture."""
        pass

    def reset(self):
        """Resets the sequence to the first frame."""
        self.current_idx = 0

    def isOpened(self) -> bool:
        """Returns True if images were loaded successfully."""
        return self.frame_count > 0

    @property
    def get_width(self) -> int:
        return self.width

    @property
    def get_height(self) -> int:
        return self.height

    @property
    def get_fps(self) -> float:
        return self.fps

    @property
    def get_frame_count(self) -> int:
        return self.frame_count
