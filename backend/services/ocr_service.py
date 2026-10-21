import logging
import asyncio
import io
import aiohttp
from typing import Optional, List
try:
    import easyocr
    import numpy as np
    from PIL import Image
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

logger = logging.getLogger("OCRService")

class OCRService:
    def __init__(self):
        self.reader = None
        self._lock = asyncio.Lock()

    async def _get_reader(self):
        if not OCR_AVAILABLE:
            raise RuntimeError("easyocr or its dependencies are not installed.")
        
        async with self._lock:
            if self.reader is None:
                logger.info("Initializing EasyOCR reader (this may take a while on first run)...")
                # Run in thread to not block event loop.
                self.reader = await asyncio.to_thread(easyocr.Reader, ['pt'], gpu=True, verbose=False)
        return self.reader

    async def extract_text_from_url(self, image_url: str) -> Optional[str]:
        """Downloads an image and extracts text from it using OCR."""
        if not image_url:
            return None

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(image_url, timeout=10) as resp:
                    if resp.status != 200:
                        logger.warning(f"Failed to fetch image {image_url}: status {resp.status}")
                        return None
                    image_bytes = await resp.read()

            # Process image in a separate thread
            return await asyncio.to_thread(self._process_image_bytes, image_bytes)
            
        except Exception as e:
            logger.error(f"OCR extraction failed for {image_url}: {e}")
            return None

    def _process_image_bytes(self, image_bytes: bytes) -> Optional[str]:
        """Synchronous part of image processing for easyocr."""
        try:
            import cv2
            image = Image.open(io.BytesIO(image_bytes))
            # Convert to RGB (in case of PNG with alpha)
            if image.mode != 'RGB':
                image = image.convert('RGB')
                
            img_array = np.array(image)
            
            # OCR-04: Resize large images to speed up processing
            max_width = 1200
            if img_array.shape[1] > max_width:
                scale = max_width / img_array.shape[1]
                dim = (max_width, int(img_array.shape[0] * scale))
                img_array = cv2.resize(img_array, dim, interpolation=cv2.INTER_AREA)

            # OCR-01: OpenCV Preprocessing for higher accuracy
            img_gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
            # Increase contrast
            img_gray = cv2.convertScaleAbs(img_gray, alpha=1.5, beta=0)
            
            # This is safe because _get_reader is called before or we initialize here
            if self.reader is None:
                logger.info("Initializing EasyOCR reader synchronously (fallback)...")
                self.reader = easyocr.Reader(['pt'], gpu=True, verbose=False)
                
            # Run OCR on the preprocessed image
            results = self.reader.readtext(img_gray)
            extracted_text = " ".join([text for _, text, _ in results])
            return extracted_text.strip().lower()
        except Exception as e:
            logger.error(f"Error processing image bytes for OCR: {e}")
            return None

    def compare_image_texts(self, reference_text: str, target_text: str) -> float:
        """
        Calculates a simple similarity score between two extracted texts.
        Returns a float between 0.0 and 1.0.
        """
        if not reference_text or not target_text:
            return 0.0

        ref_words = set(reference_text.split())
        target_words = set(target_text.split())

        if not ref_words or not target_words:
            return 0.0

        # Calculate Jaccard similarity
        intersection = ref_words.intersection(target_words)
        union = ref_words.union(target_words)
        
        return len(intersection) / len(union)

    async def download_image_bytes(self, image_url: str) -> Optional[bytes]:
        """Downloads an image and returns its bytes."""
        if not image_url:
            return None
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(image_url, timeout=10) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception as e:
            logger.error(f"Failed to download image {image_url}: {e}")
        return None

    def compare_image_visuals(self, ref_bytes: bytes, target_bytes: bytes) -> float:
        """
        Calculates structural similarity using OpenCV ORB.
        Returns a float between 0.0 and 1.0 representing the ratio of good matches.
        """
        if not ref_bytes or not target_bytes:
            return 0.0

        try:
            import cv2
            
            # Convert bytes to numpy array
            ref_arr = np.frombuffer(ref_bytes, np.uint8)
            target_arr = np.frombuffer(target_bytes, np.uint8)
            
            # Decode images
            img1 = cv2.imdecode(ref_arr, cv2.IMREAD_GRAYSCALE)
            img2 = cv2.imdecode(target_arr, cv2.IMREAD_GRAYSCALE)
            
            if img1 is None or img2 is None:
                return 0.0
                
            # Initialize ORB
            orb = cv2.ORB_create()
            
            kp1, des1 = orb.detectAndCompute(img1, None)
            kp2, des2 = orb.detectAndCompute(img2, None)
            
            if des1 is None or des2 is None or len(des1) == 0 or len(des2) == 0:
                return 0.0
                
            # Match descriptors
            bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
            matches = bf.match(des1, des2)
            
            if not matches:
                return 0.0
                
            # Good matches with distance < 50
            good_matches = [m for m in matches if m.distance < 50]
            
            # Calculate score as ratio of good matches to the minimum keypoints found (to avoid penalizing different background sizes too heavily)
            min_kp = min(len(kp1), len(kp2))
            if min_kp == 0:
                return 0.0
                
            score = len(good_matches) / min_kp
            return min(1.0, score)
            
        except Exception as e:
            logger.error(f"Error comparing visual features: {e}")
            return 0.0

ocr_service = OCRService()
