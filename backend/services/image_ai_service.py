from __future__ import annotations
import logging
import asyncio
import io
import aiohttp
from typing import Optional

logger = logging.getLogger("ImageAIService")

import logging as _logging
_ai_logger = _logging.getLogger("ImageAIService.init")

try:
    from PIL import Image
    import torch
    from transformers import CLIPProcessor, CLIPModel
    import numpy as np
    from scipy.spatial.distance import cosine
    AI_AVAILABLE = True
    _ai_logger.info(f"AI_AVAILABLE=True | torch={torch.__version__} | device will be resolved at init")
except ImportError as _e:
    AI_AVAILABLE = False
    _ai_logger.warning(f"AI_AVAILABLE=False | missing dependency: {_e.name} | install with: pip install transformers torch scipy Pillow")


class ImageAIService:
    def __init__(self):
        self.model = None
        self.processor = None
        self._lock = asyncio.Lock()
        self.device = "cpu"
        if AI_AVAILABLE:
            if torch.cuda.is_available():
                self.device = "cuda"
            elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                self.device = "mps"

    async def _initialize_model(self):
        if not AI_AVAILABLE:
            raise RuntimeError("transformers, torch ou scipy não instalados.")
        
        async with self._lock:
            if self.model is None:
                logger.info("Carregando modelo CLIP (OpenAI) via HuggingFace na memória...")
                model_id = "openai/clip-vit-base-patch32"
                self.model = await asyncio.to_thread(CLIPModel.from_pretrained, model_id)
                self.model.to(self.device)
                self.processor = await asyncio.to_thread(CLIPProcessor.from_pretrained, model_id)
                self.model.eval()
                logger.info(f"Modelo CLIP carregado com sucesso no dispositivo: {self.device}")

    async def download_image_bytes(self, image_url: str) -> Optional[bytes]:
        if not image_url:
            return None
        try:
            from curl_cffi.requests import AsyncSession
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            }
            async with AsyncSession(impersonate="chrome120") as session:
                resp = await session.get(image_url, headers=headers, timeout=15)
                if resp.status_code == 200:
                    return resp.content
                else:
                    logger.warning(f"Failed to download image {image_url}: HTTP {resp.status_code}")
        except ImportError:
            # Fallback to aiohttp if curl_cffi is not available
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(image_url, timeout=10) as resp:
                        if resp.status == 200:
                            return await resp.read()
            except Exception as e:
                logger.error(f"Fallback aiohttp failed for {image_url}: {e}")
        except Exception as e:
            logger.error(f"Failed to download image {image_url}: {e}")
        return None

    def _crop_whitespace(self, image: Image.Image) -> Image.Image:
        try:
            import cv2
            img_array = np.array(image)
            if img_array.shape[-1] == 4: # Handle RGBA
                img_array = cv2.cvtColor(img_array, cv2.COLOR_RGBA2RGB)
            gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
            # Threshold: darker than 240 is object
            _, thresh = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY_INV)
            coords = cv2.findNonZero(thresh)
            if coords is not None:
                x, y, w, h = cv2.boundingRect(coords)
                padding = 10
                x1 = max(0, x - padding)
                y1 = max(0, y - padding)
                x2 = min(img_array.shape[1], x + w + padding)
                y2 = min(img_array.shape[0], y + h + padding)
                return image.crop((x1, y1, x2, y2))
        except Exception as e:
            logger.warning(f"Falha ao recortar bordas brancas: {e}")
        return image

    def _get_image_embedding(self, img_bytes: bytes) -> Optional[np.ndarray]:
        try:
            from PIL import ImageOps
            image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            image = self._crop_whitespace(image)
            # Remove color to force structural matching over color matching
            image = ImageOps.grayscale(image).convert("RGB")
            
            inputs = self.processor(images=image, return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            
            with torch.no_grad():
                features = self.model.get_image_features(**inputs)
                
            if not isinstance(features, torch.Tensor):
                if hasattr(features, 'image_embeds'):
                    features = features.image_embeds
                elif hasattr(features, 'pooler_output'):
                    features = features.pooler_output
                else:
                    features = features[0]
                    
            features = features / features.norm(p=2, dim=-1, keepdim=True)
            return features.squeeze().cpu().numpy()
        except Exception as e:
            logger.error(f"Erro ao extrair features da imagem: {e}")
            return None

    async def get_embedding_async(self, img_bytes: bytes) -> Optional[np.ndarray]:
        if not img_bytes:
            return None
        if self.model is None:
            await self._initialize_model()
        return await asyncio.to_thread(self._get_image_embedding, img_bytes)

    def _get_image_embeddings_batch(self, images_bytes: list[bytes]) -> list[Optional[np.ndarray]]:
        try:
            from PIL import Image, ImageOps
            import torch
            
            valid_images = []
            valid_indices = []
            results = [None] * len(images_bytes)
            
            for i, img_bytes in enumerate(images_bytes):
                if not img_bytes:
                    continue
                try:
                    image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                    image = self._crop_whitespace(image)
                    image = ImageOps.grayscale(image).convert("RGB")
                    valid_images.append(image)
                    valid_indices.append(i)
                except Exception as e:
                    logger.error(f"Erro ao processar imagem index {i}: {e}")
                    
            if not valid_images:
                return results
                
            inputs = self.processor(images=valid_images, return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            
            with torch.no_grad():
                features = self.model.get_image_features(**inputs)
                
            if not isinstance(features, torch.Tensor):
                if hasattr(features, 'image_embeds'):
                    features = features.image_embeds
                elif hasattr(features, 'pooler_output'):
                    features = features.pooler_output
                else:
                    features = features[0]
                    
            features = features / features.norm(p=2, dim=-1, keepdim=True)
            features_np = features.cpu().numpy()
            
            for j, idx in enumerate(valid_indices):
                results[idx] = features_np[j]
                
            return results
        except Exception as e:
            logger.error(f"Erro ao extrair features em lote: {e}")
            return [None] * len(images_bytes)

    async def get_embeddings_batch_async(self, images_bytes: list[bytes]) -> list[Optional[np.ndarray]]:
        if not images_bytes:
            return []
        if self.model is None:
            await self._initialize_model()
        return await asyncio.to_thread(self._get_image_embeddings_batch, images_bytes)

    async def calculate_score_from_embeddings(self, ref_embed: np.ndarray, target_embed: np.ndarray) -> float:
        if ref_embed is None or target_embed is None:
            return 0.0
        similarity = 1.0 - cosine(ref_embed, target_embed)
        return max(0.0, float(similarity))

    async def calculate_image_score(self, ref_bytes: bytes, target_bytes: bytes) -> float:
        if not ref_bytes or not target_bytes:
            return 0.0

        if self.model is None:
            await self._initialize_model()

        ref_embed = await asyncio.to_thread(self._get_image_embedding, ref_bytes)
        target_embed = await asyncio.to_thread(self._get_image_embedding, target_bytes)

        if ref_embed is None or target_embed is None:
            return 0.0

        similarity = 1.0 - cosine(ref_embed, target_embed)
        return max(0.0, float(similarity))

image_ai_service = ImageAIService()
