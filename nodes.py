"""
ArtemKo7v Image Catalog Indexed -- backend part.

Add description here

"""

import os

import numpy as np
import torch
from PIL import Image, ImageOps

class ArtemKo7vImageCatalogIndexed:

NODE_CLASS_MAPPINGS = {
    "ArtemKo7vImageCatalogIndexed": ArtemKo7vImageCatalogIndexed,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ArtemKo7vImageCatalogIndexed": "Image Catalog Indexed",
}
