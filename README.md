# 🎮 FF ID Store Bot

Telegram bot for Free Fire ID selling with FamGateway UPI auto-payment.

## Features
- Admin panel with inline buttons (Add / View / Edit / Delete posts)
- Buyer flow: Post ID → FamGateway QR → Auto payment verify → Credentials send
- No database — all data in memory (bot restart se reset)
- Railway deploy ready

## Setup

### 1. Bot banao
1. [@BotFather](https://t.me/BotFather) pe jao → `/newbot`
2. Token copy karo

### 2. FamGateway API Key
1. [famgateway.in](https://famgateway.in) pe account banao
2. FamPay Gmail connect karo (IMAP enable karo pehle)
3. API Key copy karo dashboard se

### 3. Railway Deploy
1. GitHub pe ye repo push karo
2. [railway.app](https://railway.app) pe jao → New Project → Deploy from GitHub
3. **Variables** me ye 3 add karo:

| Variable | Value |
|----------|-------|
| `BOT_TOKEN` | BotFather se mila token |
| `ADMIN_ID` | Tera Telegram numeric ID |
| `FG_API_KEY` | FamGateway API key |

4. **Service Type**: Worker (Procfile me already hai)
5. Deploy!

### Apna Telegram ID kaise pata kare?
[@userinfobot](https://t.me/userinfobot) pe `/start` bhejo → ID milega

## Bot Usage

### Admin (sirf tu)
- `/start` → Admin Panel button dikhega
- Posts add/edit/delete karo inline buttons se
- Har post me: Post ID + Price + Login Credentials

### Buyer
- `/start` → Buy FF ID button
- Post ID type karo
- QR scan karo ya Pay Page open karo
- Payment ke baad credentials auto-aate hain

## Files
```
ff-store-bot/
├── bot.py          # Main bot code
├── requirements.txt
├── Procfile        # Railway worker config
├── runtime.txt     # Python version
└── .gitignore
```
