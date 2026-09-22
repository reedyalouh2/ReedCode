#!/bin/bash
sed -i 's/price \* discount_percent/price * (discount_percent \/ 100)/' /app/pricing.py
