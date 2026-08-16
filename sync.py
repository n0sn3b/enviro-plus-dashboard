"""
Sync module — copies the SQLite database to a remote NAS via SCP.

Designed to be resilient:
  - Non-blocking (runs in background thread)
  - Retries on failure
  - Logs errors but never crashes the main app
  - Gracefully degrades if NAS is unreachable
"""

import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


class NasSync:
    """
    Periodically sync the SQLite DB to a remote NAS via SCP.

    Usage:
        sync = NasSync(db_path="/path/to/db", remote="user@nas:/backup/db")
        sync.start()

        # Later...
        sync.stop()
    """

    def __init__(self, db_path, remote, interval=300, max_retries=3,
                 enabled=True):
        self.db_path = db_path
        self.remote = remote
        self.interval = interval
        self.max_retries = max_retries
        self.enabled = enabled
        self._thread = None
        self._running = False
        self._last_success = None
        self._consecutive_failures = 0

    @property
    def is_synced(self):
        """Whether at least one successful sync has occurred."""
        return self._last_success is not None

    @property
    def last_sync_time(self):
        """Timestamp of last successful sync, or None."""
        return self._last_success

    def start(self):
        """Start the background sync thread."""
        if not self.enabled:
            logger.info("NAS sync disabled in config")
            return
        if not self.remote:
            logger.warning("NAS sync: remote target not configured")
            return
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        logger.info(f"NAS sync started: {self.remote} every {self.interval}s")

    def stop(self):
        """Stop the background sync thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("NAS sync stopped")

    def _sync_loop(self):
        """Main loop: sync at configured interval."""
        while self._running:
            try:
                self._do_sync()
            except Exception as e:
                logger.error(f"Sync error: {e}")
            # Wait for next interval (or exit cleanly)
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

    def _do_sync(self):
        """
        Attempt to SCP the DB to the remote NAS.
        Retries up to max_retries times on failure.
        """
        if not os.path.exists(self.db_path):
            logger.warning(f"DB file not found: {self.db_path}")
            return

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug(f"Sync attempt {attempt}/{self.max_retries}")
                result = subprocess.run(
                    ["scp", self.db_path, self.remote],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    self._last_success = time.time()
                    self._consecutive_failures = 0
                    logger.info("Sync successful")
                    return
                else:
                    err = result.stderr.strip()
                    logger.warning(f"Sync attempt {attempt} failed: {err}")
            except subprocess.TimeoutExpired:
                logger.warning(f"Sync attempt {attempt} timed out")
            except FileNotFoundError:
                logger.error("scp command not found — install openssh-client")
                return  # No point retrying if scp doesn't exist

        self._consecutive_failures += 1
        logger.error(
            f"Sync failed after {self.max_retries} attempts "
            f"({self._consecutive_failures} consecutive failures)"
        )

    def force_sync(self):
        """Manually trigger a sync now (blocks until done or failed)."""
        try:
            self._do_sync()
        except Exception as e:
            logger.error(f"Force sync error: {e}")
            raise
