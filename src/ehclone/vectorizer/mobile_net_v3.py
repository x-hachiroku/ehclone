from functools import cache

import io
from PIL import Image
import numpy as np

from ehclone.logger import logger


class Vectorizer:
    def __init__(self):
        from imagededup.methods import CNN

        self.cnn = CNN()
        logger.info('Initialized MobileNetV3 vectorizer.')

    def encode(self, image_bytes: bytes) -> list[float] | None:
        try:
            pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            image_array = np.array(pil_image)
            vector = self.cnn.encode_image(image_array=image_array).flatten()
            norm = np.linalg.norm(vector)
            if vector.size != 576 or not np.all(np.isfinite(vector)) or norm <= 0:
                raise ValueError('Invalid MobileNetV3 vector')
            return (vector / norm).tolist()
        except Exception:
            logger.exception('Error vectorizing thumbnail bytes')
            return None


@cache
def get_vectorizer() -> Vectorizer:
    """Load the model once, only when an image actually needs encoding."""
    return Vectorizer()


if __name__ == '__main__':
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description='Compute the MobileNetV3 cosine similarity.'
    )
    parser.add_argument('img1', type=Path)
    parser.add_argument('img2', type=Path)
    args = parser.parse_args()

    try:
        image_bytes = [path.read_bytes() for path in (args.img1, args.img2)]
    except OSError as exc:
        parser.error(str(exc))

    vectorizer = get_vectorizer()
    vectors = [vectorizer.encode(data) for data in image_bytes]
    if any(vector is None for vector in vectors):
        parser.error('Could not encode one or both images')

    # encode() returns unit vectors, so their dot product is cosine similarity.
    similarity = float(np.clip(np.dot(vectors[0], vectors[1]), -1.0, 1.0))

    logger.info('Cosine distance: {:.6f}', 1 - similarity)
