from .base import BidResult, BiddingPlatform, Job
from .ots import OTSPlatform

# name in config.yaml `bidding:` -> class
PLATFORMS = {
    "ots": OTSPlatform,
}

__all__ = ["PLATFORMS", "BiddingPlatform", "Job", "BidResult"]
