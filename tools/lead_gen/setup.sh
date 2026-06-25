#!/usr/bin/env bash
# Setup script for Spruce My Site — Local Website Lead Generator

set -e

echo "================================================"
echo "  Spruce My Site — Lead Generator Setup"
echo "================================================"

echo ""
echo "[1/2] Installing Python dependencies..."
pip install requests beautifulsoup4 --quiet

echo ""
echo "[2/2] Verifying install..."
python3 -c "import requests, bs4; print('  ✓ requests', requests.__version__); print('  ✓ beautifulsoup4', bs4.__version__)"

echo ""
echo "================================================"
echo "  Setup complete!"
echo ""
echo "  Usage:"
echo '  python -m tools.lead_gen --location "Manchester, UK" --category restaurant'
echo '  python -m tools.lead_gen --location "Austin, TX" --category dentist --radius 10'
echo '  python -m tools.lead_gen --location "London, UK" --category gym --limit 30 --output ./leads'
echo ""
echo "  Categories:"
python3 -c "
from tools.lead_gen.lead_gen import CATEGORIES
cats = sorted(CATEGORIES.keys())
for i in range(0, len(cats), 5):
    print('  ' + '  '.join(cats[i:i+5]))
" 2>/dev/null || echo "  restaurant, cafe, bar, dentist, gym, lawyer, mechanic, ..."
echo ""
echo "  Spruce My Site Services detected:"
echo "    SSL/HTTPS, Mobile Design, Speed Optimisation, SEO Setup,"
echo "    Schema Markup, Analytics, Social Integration, Contact Page,"
echo "    Live Chat, CTA/Lead Form, Google Maps, Email Marketing,"
echo "    Favicon, Modern CMS/Design"
echo "================================================"
