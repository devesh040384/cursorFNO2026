import time
import threading
import logging

class RateLimitedAPI:
    def __init__(self, smart_api, max_calls_per_sec=1):
        self.api = smart_api
        self.delay = 1.0 / max_calls_per_sec
        self.last_call = 0
        self.lock = threading.Lock()

    def __getattr__(self, name):
        """Intercepts all method calls to the SmartAPI instance"""
        attr = getattr(self.api, name)
        
        if callable(attr):
            def wrapper(*args, **kwargs):
                with self.lock:
                    now = time.time()
                    elapsed = now - self.last_call
                    if elapsed < self.delay:
                        time.sleep(self.delay - elapsed)
                    try:
                        return attr(*args, **kwargs)
                    finally:
                        self.last_call = time.time()
            return wrapper
            
        return attr
