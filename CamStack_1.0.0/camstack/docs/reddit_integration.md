# Reddit Nature Webcam Discovery

## Overview
The fallback system has been enhanced to automatically discover popular nature webcams from Reddit discussions. This allows CamStack to dynamically find and prioritize nature streams based on community engagement.

## How It Works

### Data Sources
The system searches multiple Reddit sources for nature webcam discussions:
- **Subreddits**: r/NatureLive, r/livestreaming, r/nature, r/camping, r/Outdoors, r/wildlife
- **Search Queries**: "nature webcam", "wildlife webcam", "live nature stream", "animal webcam", "nature cam live"

### Popularity Ranking
URLs discovered from Reddit are ranked by a popularity score calculated as:
```
popularity = upvotes + number_of_comments
```

Higher scores indicate more popular discussions, which likely point to better quality streams.

### Caching
- Reddit results are cached for **2 hours** by default
- Cache file: `/opt/camstack/runtime/reddit_cams.json`
- This reduces API calls and improves performance

## Functions Added

### `get_reddit_nature_cams(use_cache=True, max_age=7200)`
Discovers and returns a list of YouTube URLs from Reddit discussions.

**Parameters:**
- `use_cache`: Whether to use cached results (default: True)
- `max_age`: Maximum cache age in seconds (default: 7200 = 2 hours)

**Returns:**
- List of YouTube URLs, ranked by Reddit popularity

### Updated Functions

#### `get_featured_fallback_url(use_reddit=True)`
Enhanced to include Reddit-discovered URLs.

**Behavior:**
- If Reddit URLs available: 70% chance of picking from Reddit, 30% from hardcoded sources
- If no Reddit URLs: Falls back to hardcoded EXPLORE_LIVE_URLS

#### `get_best_live_stream(max_candidates=30, exclude=None, use_reddit=True)`
Enhanced to prioritize Reddit-discovered streams.

**Parameters:**
- `use_reddit`: If True, includes Reddit URLs (prioritized first)

## Testing

Run the test script to verify Reddit integration:
```bash
python3 /home/pi/test_reddit_fallback.py
```

## Current Status

✅ **Working!** The test found 11 nature webcam URLs from Reddit discussions.

Example of discovered URLs:
- Live bird feeders
- Wildlife sanctuary cams
- Nature landscape streams
- Animal rehabilitation center cams

## Benefits

1. **Dynamic Discovery**: Automatically finds new popular streams
2. **Community-Driven**: Uses real user engagement to rank quality
3. **Diverse Sources**: Expands beyond hardcoded streams
4. **Fresh Content**: Discovers trending streams every 2 hours
5. **Backward Compatible**: Existing code works without changes (use_reddit defaults to True)

## Monitoring

To see what Reddit URLs are currently cached:
```bash
cat /opt/camstack/runtime/reddit_cams.json | python3 -m json.tool
```

To force a fresh Reddit search (bypass cache):
```python
from fallback import get_reddit_nature_cams
urls = get_reddit_nature_cams(use_cache=False)
```

## Notes

- Uses Reddit's public JSON API (no authentication required)
- Respects Reddit's rate limiting with User-Agent header
- Falls back gracefully if Reddit is unavailable
- Existing hardcoded URLs still available as fallback
