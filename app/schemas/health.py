from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]


class DatabaseHealthResponse(BaseModel):
    status: Literal["ok", "error"]
    database: Literal["connected", "unavailable"]
