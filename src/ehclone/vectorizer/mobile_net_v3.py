import numpy as np
from imagededup.methods import CNN

from ehclone.config import config
from ehclone.logger import logger

class Vectorizer:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Vectorizer, cls).__new__(cls)
            cls._instance._init_model()
        return cls._instance

    def _init_model(self):
        self.cnn = CNN()
        logger.info('Initialized MobileNetV3 vectorizer.')

    def encode(self, image_path):
        try:
            image_path = str(image_path)
            vector = self.cnn.encode_image(image_file=image_path)
            vector = vector.flatten()
            norm = np.linalg.norm(vector)
            if norm == 0:
                raise ValueError('Vector norm is zero')
            return (vector / norm).tolist()
        except Exception as e:
            logger.error(f'Error vectorizing {image_path}: {e}')
            return None


vectorizer = Vectorizer()
