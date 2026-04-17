#!/bin/bash
# Build script for Render
pip install -r requirements.txt

# Build React frontend
cd frontend
npm install
npm run build
cd ..
