import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    logger.info("Worker starting...")
    # Job queue consumer will be implemented here
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
