import os
import logging
from logging.handlers import RotatingFileHandler
import traceback
import sys

class ErrorSentinel:
    def __init__(self, log_directory: str = "logs", max_bytes: int = 5 * 1024 * 1024, backup_count: int = 5):
        """
        Sets up robust, rolling log directories on disk.
        :param max_bytes: Max file size before rolling over (Default: 5MB)
        :param backup_count: Number of historical log files to retain
        """
        self.log_directory = log_directory
        if not os.path.exists(self.log_directory):
            os.makedirs(self.log_directory)
            
        self.logger = logging.getLogger("FO_Bot_Sentinel")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers = [] # Clear duplicates
        
        # 1. Standard Terminal Output Layout
        console_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)
        
        # 2. Production File Storage Layout (With automatic 5MB rotation guardrails)
        file_formatter = logging.Formatter("%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s")
        file_handler = RotatingFileHandler(
            os.path.join(self.log_directory, "bot_runtime.log"),
            maxBytes=max_bytes,
            backupCount=backup_count
        )
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)

    def log_exception(self, context_message: str, exception_object: Exception):
        """Captures full stack traces and preserves them on disk without interrupting runtime threads."""
        err_msg = (
            f"❌ CRITICAL ERROR DURING: {context_message}\n"
            f"Exception Detail: {str(exception_object)}\n"
            f"{'='*40}\n"
            f"{traceback.format_exc()}"
            f"{'='*40}"
        )
        self.logger.critical(err_msg)

    def handle_network_drop(self, current_retry_attempt: int) -> bool:
        """Determines reconnection viability based on structural limits."""
        self.logger.warning(f"🔌 Network discontinuity encountered. Attempting recovery loop: Attempt #{current_retry_attempt}")
        if current_retry_attempt > 5:
            self.logger.critical("🚨 Maximum recovery thresholds breached. Stopping bot execution to protect accounts.")
            return False
        return True

# --- RUNNER TESTING HARNESS ---
if __name__ == "__main__":
    sentinel = ErrorSentinel()
    sentinel.logger.info("Error Sentinel framework successfully integrated.")
    
    # Simulating a runtime exception parsing bad data
    try:
        simulated_bad_calculation = 100 / 0
    except Exception as e:
        sentinel.log_exception("Calculating Position Moneyness Matrix", e)
