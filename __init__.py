from fastapi import APIRouter
from lnbits.db import Database

from .views import denchi_ext_generic
from .views_api import denchi_ext_api

db = Database("ext_denchi")

denchi_static_files = [
    {
        "path": "/denchi/static",
        "name": "denchi_static",
    }
]

denchi_ext: APIRouter = APIRouter(prefix="/denchi", tags=["denchi"])
denchi_ext.include_router(denchi_ext_generic)
denchi_ext.include_router(denchi_ext_api)

__all__ = ["db", "denchi_ext", "denchi_static_files"]
