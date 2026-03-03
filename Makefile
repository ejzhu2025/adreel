.PHONY: install init demo test clean lint

# ── Setup ──────────────────────────────────────────────────────────────────────
install:
	pip install -e ".[dev]" 2>/dev/null || pip install -e .
	pip install -r requirements.txt
	cp -n .env.example .env || true

# ── Run ────────────────────────────────────────────────────────────────────────
init:
	python -m cli.main init

demo:
	python -m cli.main demo

# ── Dev ────────────────────────────────────────────────────────────────────────
test:
	python -m pytest tests/ -v

test-fast:
	python -m pytest tests/ -v -x --tb=short

lint:
	python -m ruff check . --fix 2>/dev/null || echo "ruff not installed, skipping"

# ── Cleanup ────────────────────────────────────────────────────────────────────
clean:
	rm -rf data/projects data/exports data/chroma
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true

clean-all: clean
	rm -rf data/

# ── Quick project commands ─────────────────────────────────────────────────────
new-tong-sui:
	python -m cli.main new \
		--brief "Create a summer promo video for Tong Sui's new drink Coconut Watermelon Refresh." \
		--brand tong_sui \
		--user ej
