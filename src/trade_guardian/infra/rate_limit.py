import time


class RateLimiter:
    def __init__(self, throttle_sec: float):
        self.throttle_sec = float(throttle_sec)

    def sleep(self):
        if self.throttle_sec > 0:
            time.sleep(self.throttle_sec)
