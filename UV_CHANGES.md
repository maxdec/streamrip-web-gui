# Recent Changes

## 1. UV Refactoring (Completed)

Added `uv` for faster dependency management.

**Files added:**
- `pyproject.toml` - Defines dependencies
- `.python-version` - Python 3.11
- `setup-dev.sh` - Quick setup script
- Updated `Dockerfile` to use uv

**Quick Start:**
```bash
./setup-dev.sh
source .venv/bin/activate
python app.py
```

## 2. Streamrip Library Integration (In Progress)

Changed from subprocess calls to using streamrip as a Python library.

### What Changed

**Search functionality:**
- Removed subprocess calls to `rip search`
- Removed temp file handling
- Now uses streamrip client library directly
- More fields available (album art, year, label, track count, etc.)
- Better error handling

**Files modified:**
- `app.py` - Added streamrip library imports and refactored search

**Note:** Fixed `Config` initialization - it's `Config(path)` not `Config.from_toml()`. The config data is accessed via `config.session`.

### Benefits

- No more temp files or `--output-file` workarounds
- Direct access to full metadata
- Cleaner error handling
- Async support for better performance
- Access to more fields (cover art, year, label, etc.)

### How It Works Now

```python
# Old way (subprocess)
subprocess.run(["rip", "search", "--output-file", tmp, source, type, query])
# Parse JSON from temp file

# New way (library)
config = Config(STREAMRIP_CONFIG)
client = QobuzClient(config.session)
await client.login()
results = await client.search(search_type, query, limit=100)
# Direct access to full metadata
```

### Next Steps

- Test the search functionality
- Refactor download functionality to use library
- Remove remaining subprocess calls

### Testing

Try searching for something:
```bash
# Start the app
python app.py

# In browser, go to http://localhost:5000
# Search for albums/tracks on Qobuz/Deezer
```

## Docker

Nothing changed! Just rebuild:
```bash
docker-compose up -d --build
```
