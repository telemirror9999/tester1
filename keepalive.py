
import asyncio
import aiohttp
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class KeepAliveService:
    def __init__(self, url: str, interval_minutes: int = 5):
        self.url = url
        self.interval_seconds = interval_minutes * 60
        self.is_running = False
        
    async def ping_self(self):
        """Ping the service to keep it alive"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.url}/health", timeout=30) as response:
                    if response.status == 200:
                        logger.info(f"✅ Keep-alive ping successful at {datetime.now()}")
                        return True
                    else:
                        logger.warning(f"⚠️ Keep-alive ping returned status {response.status}")
                        return False
        except Exception as e:
            logger.error(f"❌ Keep-alive ping failed: {e}")
            return False
    
    async def start_keep_alive(self):
        """Start the keep-alive loop"""
        if self.is_running:
            return
            
        self.is_running = True
        logger.info(f"🚀 Starting keep-alive service (ping every {self.interval_seconds/60} minutes)")
        
        while self.is_running:
            try:
                await self.ping_self()
                await asyncio.sleep(self.interval_seconds)
            except Exception as e:
                logger.error(f"❌ Keep-alive service error: {e}")
                await asyncio.sleep(60)  # Wait 1 minute before retrying
    
    def stop_keep_alive(self):
        """Stop the keep-alive service"""
        self.is_running = False
        logger.info("🛑 Keep-alive service stopped")
