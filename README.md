# W1NNURS SUPPLY Bot v3

Available Stock now starts with brand buttons and reads multiple Google Sheet tabs.

Brands/tabs:
- ALO -> Alo
- ALOCS -> ALOCS
- BAPE -> BAPE
- CPFM -> CPFM
- DENIM TEARS -> DENIM TEARS
- KITH -> Kith
- NIKE -> NIKE CLO
- STUSSY -> STUSSY
- SUPREME -> SUPREME
- TRAVIS -> TRAVIS
- YZY -> YZY

Inside each brand:
- only products with stock > 0 are shown
- only available sizes are shown
- PRICE NET is never displayed
- Ask Price sends the request to admins
- admins can reply with a custom price

Deploy:
1. Replace bot.py in GitHub with this v3 version.
2. Replace requirements.txt if needed.
3. Railway redeploys automatically.
4. Existing Railway variables stay the same.
5. WORKSHEET_GID is no longer needed in v3.
