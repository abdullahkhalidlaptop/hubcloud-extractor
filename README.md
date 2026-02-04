Render + Vercel Playwright deployment (step-by-step, simple)

Overview:
- Render: runs the full Playwright service (render_service.py) — behaves exactly like your PC.
- Vercel: hosts api/proxy.py, which forwards requests to Render and wakes it when sleeping.

You will:
1) Create a Git repo with these files.
2) Deploy the Playwright service to Render (Docker).
3) Deploy the proxy to Vercel and set RENDER_SERVICE_URL to your Render URL.
4) Call your Vercel URL: https://<your-vercel-app>.vercel.app/api/hubcloud?url=<hubcloud_link>

Step-by-step (for absolute beginners):

A) Prepare files locally
1. Create a new folder and put all files from this repo into it.
2. Initialize Git:
   - git init
   - git add .
   - git commit -m "initial commit"

B) Push code to GitHub (Render and Vercel will read from Git)
1. Create a new GitHub repository (public or private).
2. Follow instructions to add remote and push:
   - git remote add origin https://github.com/yourname/yourrepo.git
   - git branch -M main
   - git push -u origin main

C) Deploy service to Render (Playwright-enabled)
1. Create a free Render account: https://dashboard.render.com
2. Click "New" → "Web Service".
3. Connect your GitHub repo and select the repo and branch (main).
4. For "Environment" choose "Docker" (Render will detect Dockerfile).
5. Use default settings initially. Recommended: increase instance memory to 1GB if available.
6. Click "Create Web Service" and wait for build + deploy.
7. After deploy, note the service URL (e.g. https://your-app.onrender.com).

D) Configure Vercel proxy
1. Create a free Vercel account: https://vercel.com
2. Create a new project and import the same GitHub repo OR create a separate repo only containing api/proxy.py + vercel.json.
   - If you use the same repo, Vercel will only build the api/ files.
3. In Vercel Project → Settings → Environment Variables:
   - Add RENDER_SERVICE_URL = https://your-app.onrender.com
   - (Optional) add RETRY_AFTER = 30
   - (Optional) add MAX_BLOCK_WAIT = 20
4. Deploy the Vercel project (via UI or vercel CLI).

E) Test
1. Wait until both services show deployed.
2. Try the Vercel proxy:
   - curl "https://<your-vercel-app>.vercel.app/api/hubcloud?url=<hubcloud_link>"
   - If Render was sleeping, you'll get 202 with Retry-After. Wait ~30s and retry.
3. Alternatively test Render directly:
   - curl "https://your-app.onrender.com/api/hubcloud?url=<hubcloud_link>"

Notes:
- Render free instances may sleep after ~15 minutes idle. The proxy wakes it and returns 202; client should retry after Retry-After seconds.
- For automated uptime, you can add a periodic ping (UptimeRobot, cron job) to /health or /wake.
- If you want to run locally (development), install Python packages and run:
  - pip install -r requirements.txt
  - playwright install
  - uvicorn render_service:app --reload