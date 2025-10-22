# 🚀 TrainFlow - Backtesting Platform Integration Guide

This guide explains how to integrate the Python backtesting backend with your existing Next.js project.

**Owned and developed by SkyEx Corporation**

## ✨ What's Been Added

### 1. **New Backtest Tab**
- Added to the dashboard preview mode toggles
- Positioned to the left of Chart and Code toggles as requested
- Integrates seamlessly with your existing UI

### 2. **BacktestInterface Component**
- Full-featured backtesting interface
- Real-time WebSocket updates
- Beautiful charts and metrics
- Follows your existing design system (shadcn/ui + Tailwind)

### 3. **Configuration System**
- Centralized backtest configuration
- Environment variable support
- Easy backend URL management

## 🛠️ Setup Instructions

### Step 1: Start Your Python Backend

```bash
# In your backtest-py directory
cd backend
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

### Step 2: Configure Environment Variables

Create or update your `.env.local` file in the Next.js project root:

```bash
# Backtest Backend Configuration
NEXT_PUBLIC_BACKTEST_API_URL=http://localhost:8000
NEXT_PUBLIC_BACKTEST_WS_URL=ws://localhost:8000
```

### Step 3: Test the Integration

1. **Start your Next.js app:**
   ```bash
   npm run dev
   ```

2. **Navigate to Dashboard** (`/dashboard`)

3. **Click the "Backtest" tab** (new button to the left of Chart/Code)

4. **Configure your strategy** and click "Start Backtest"

## 🔧 How It Works

### **Architecture:**
```
┌─────────────────┐    WebSocket    ┌─────────────────┐
│   Next.js App   │ ←────────────→  │ Python Backend  │
│   (Frontend)    │    HTTP API     │  (Backtesting)  │
└─────────────────┘                 └─────────────────┘
```

### **Data Flow:**
1. **User configures** strategy parameters in React
2. **WebSocket connection** established to Python backend
3. **Real-time updates** stream backtest progress
4. **Results displayed** in beautiful React components

## 🎯 Features

### **Strategy Support:**
- ✅ Moving Average Crossover
- ✅ RSI Strategy  
- ✅ Bollinger Bands Strategy
- 🔄 Easy to add more strategies

### **Real-time Features:**
- 📊 Live progress updates
- 💰 Real-time equity tracking
- 📈 Performance metrics
- 🎯 Trade execution simulation

### **UI Components:**
- 🎨 Follows your existing design system
- 📱 Responsive design
- 🚀 Smooth animations
- 💡 Intuitive user experience

## 🔌 API Endpoints Used

Your Python backend provides these endpoints:

- `GET /api/strategies` - Available strategies
- `GET /api/symbols` - Available symbols
- `POST /api/backtest` - Start backtest
- `WS /ws/backtest` - Real-time updates

## 🚨 Troubleshooting

### **Common Issues:**

1. **"Connection Error"**
   - Ensure Python backend is running on port 8000
   - Check firewall settings
   - Verify environment variables

2. **"WebSocket failed to connect"**
   - Backend must support WebSocket connections
   - Check CORS settings in Python backend

3. **"Strategy not found"**
   - Verify strategy names match between frontend and backend
   - Check strategy parameter validation

### **Debug Mode:**
Open browser console to see detailed connection logs and error messages.

## 🔄 Customization

### **Adding New Strategies:**
1. Update `src/config/backtest.ts`
2. Add strategy parameters
3. Update Python backend strategy classes

### **Modifying UI:**
- All components use your existing UI library
- Easy to customize with Tailwind classes
- Follows your project's design patterns

### **Backend Integration:**
- Python backend is completely independent
- Can run on different servers
- Easy to scale horizontally

## 📚 Next Steps

1. **Test the integration** with your existing strategies
2. **Customize the UI** to match your brand
3. **Add more strategies** as needed
4. **Deploy both services** to production

## 🆘 Need Help?

- Check the browser console for error messages
- Verify Python backend logs
- Ensure all environment variables are set
- Test WebSocket connection separately

---

**🎉 Congratulations!** You now have TrainFlow, a fully integrated backtesting platform by SkyEx Corporation, in your Next.js dashboard!