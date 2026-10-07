import os
import shutil
import uuid
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from db.models.trade_image import TradeImage
from db.session import get_db

router = APIRouter(prefix="/trade-images")

@router.post("/images")
async def upload_images(
    trade_id: int = Form(...),
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db)
):
    save_dir = f"uploads/trade_{trade_id}"
    os.makedirs(save_dir, exist_ok=True)

    saved_images = []

    for file in files:
        file_path = f"{save_dir}/{uuid.uuid4()}_{file.filename}"

        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        image = TradeImage(
            trade_id=trade_id,
            file_path=file_path
        )

        db.add(image)
        saved_images.append(image)

    await db.commit()

    return {"count": len(saved_images)}

@router.delete("/{image_id}")
async def update_note(image_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.delete(
        select(TradeImage).where(TradeImage.id == image_id)
    )
    note = result.scalar_one_or_none()

    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    await db.commit()

    return note
