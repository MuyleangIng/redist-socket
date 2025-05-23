#!/bin/bash

# Activate Python virtual environment (if applicable)
source venv/bin/activate

# Change to your project directory
#cd ~/python

# Start Uvicorn
uvicorn main:app --host 0.0.0.0 --port 3039 --reload

