# To sprawi, że moduły będą dostępne przez import relative
from .database import get_db
from . import models, schemas, crud

# Eksportuj ważne elementy
__all__ = ['get_db', 'get_db', 'models', 'schemas', 'crud']