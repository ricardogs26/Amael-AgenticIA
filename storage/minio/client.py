"""
Cliente MinIO (S3-compatible) para almacenamiento de objetos.

Usado para:
  - Documentos subidos por usuarios (PDFs, DOCX)
  - Artefactos generados por agentes
"""
from __future__ import annotations

import logging
from typing import IO

from minio import Minio
from minio.error import S3Error

logger = logging.getLogger("storage.minio")

_client: Minio | None = None
_default_bucket: str = "amael-uploads"


def init_client(
    endpoint: str,
    access_key: str,
    secret_key: str,
    secure: bool = False,
    bucket: str = "amael-uploads",
) -> Minio:
    """
    Inicializa el cliente MinIO y crea el bucket por defecto si no existe.
    """
    global _client, _default_bucket
    _client = Minio(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )
    _default_bucket = bucket

    # Crear bucket si no existe
    try:
        if not _client.bucket_exists(bucket):
            _client.make_bucket(bucket)
            logger.info(f"Bucket '{bucket}' creado en MinIO.")
        else:
            logger.info(f"Bucket '{bucket}' ya existe en MinIO.")
    except S3Error as e:
        logger.warning(f"No se pudo verificar/crear bucket '{bucket}': {e}")

    logger.info(
        "Cliente MinIO inicializado",
        extra={"endpoint": endpoint, "bucket": bucket},
    )
    return _client


def get_client() -> Minio:
    """Retorna el cliente MinIO existente."""
    if _client is None:
        raise RuntimeError(
            "El cliente MinIO no está inicializado. "
            "Llama a init_client() primero."
        )
    return _client


def upload_file(
    object_name: str,
    data: IO,
    length: int,
    content_type: str = "application/octet-stream",
    bucket: str | None = None,
) -> str:
    """
    Sube un archivo a MinIO.

    Returns:
        URL del objeto (interno, no público).
    """
    bucket = bucket or _default_bucket
    get_client().put_object(
        bucket_name=bucket,
        object_name=object_name,
        data=data,
        length=length,
        content_type=content_type,
    )
    logger.info(f"Archivo subido a MinIO: {bucket}/{object_name}")
    return f"minio://{bucket}/{object_name}"


def health_check() -> bool:
    """Verifica conectividad con MinIO."""
    try:
        get_client().list_buckets()
        return True
    except Exception as e:
        logger.error(f"Health check MinIO falló: {e}")
        return False
