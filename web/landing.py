_LANDING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>AdReel — AI Video Ads in Minutes</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; -webkit-font-smoothing: antialiased; }
  body {
    background: #000;
    color: #fff;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", system-ui, sans-serif;
    overflow-x: hidden;
  }

  /* ── Typography ── */
  .t-headline {
    font-size: clamp(48px, 7.5vw, 88px);
    font-weight: 700;
    letter-spacing: -0.04em;
    line-height: 1.04;
  }
  .t-subhead {
    font-size: clamp(18px, 2.2vw, 22px);
    font-weight: 400;
    letter-spacing: -0.01em;
    line-height: 1.5;
    color: rgba(255,255,255,.55);
  }
  .t-label {
    font-size: 12px;
    font-weight: 500;
    letter-spacing: .1em;
    text-transform: uppercase;
    color: rgba(255,255,255,.3);
  }
  .t-caption {
    font-size: 13px;
    color: rgba(255,255,255,.4);
    letter-spacing: -.01em;
    line-height: 1.5;
  }

  /* ── Gradient text ── */
  .g-white {
    background: linear-gradient(180deg, #fff 60%, rgba(255,255,255,.5) 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .g-blue {
    background: linear-gradient(135deg, #3b82f6, #8b5cf6);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }

  /* ── Nav ── */
  nav {
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    height: 52px;
    display: flex; align-items: center;
    padding: 0 32px;
    backdrop-filter: blur(24px) saturate(180%);
    -webkit-backdrop-filter: blur(24px) saturate(180%);
    background: rgba(0,0,0,.7);
    border-bottom: .5px solid rgba(255,255,255,.08);
  }
  .nav-logo {
    display: flex; align-items: center; gap: 0;
    text-decoration: none;
    font-size: 20px; letter-spacing: -.04em; line-height: 1;
  }
  .nav-logo .w-ad {
    font-weight: 300; color: rgba(255,255,255,.45);
  }
  .nav-logo .w-reel {
    font-weight: 700; color: #fff;
  }
  .nav-links {
    position: absolute; left: 50%; transform: translateX(-50%);
    display: flex; gap: 28px;
  }
  .nav-link {
    font-size: 13px; color: rgba(255,255,255,.6);
    text-decoration: none; transition: color .15s;
  }
  .nav-link:hover { color: #fff; }
  .nav-cta {
    margin-left: auto;
    background: #fff; color: #000;
    font-size: 13px; font-weight: 600;
    padding: 6px 16px; border-radius: 980px;
    text-decoration: none;
    transition: opacity .2s;
  }
  .nav-cta:hover { opacity: .85; }

  /* ── Sections ── */
  section { position: relative; }

  /* ── Hero ── */
  .hero {
    min-height: 100svh;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    text-align: center;
    padding: 100px 24px 80px;
    overflow: hidden;
  }
  .hero-bg {
    position: absolute; inset: 0; pointer-events: none;
    background:
      radial-gradient(ellipse 100% 70% at 50% -5%, rgba(120,80,255,.14) 0%, transparent 65%),
      radial-gradient(ellipse 60% 50% at 85% 30%, rgba(59,130,246,.08) 0%, transparent 60%),
      radial-gradient(ellipse 50% 40% at 15% 70%, rgba(139,92,246,.06) 0%, transparent 60%);
  }
  /* Noise grain overlay */
  .hero-bg::after {
    content: '';
    position: absolute; inset: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E");
    opacity: .4;
  }

  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 14px; border-radius: 980px;
    border: .5px solid rgba(255,255,255,.14);
    background: rgba(255,255,255,.04);
    font-size: 12px; font-weight: 500;
    color: rgba(255,255,255,.55);
    margin-bottom: 32px;
    letter-spacing: .02em;
  }
  .badge-dot { width: 6px; height: 6px; border-radius: 50%; background: #34d399; flex-shrink: 0; }

  /* ── URL input ── */
  .url-wrap {
    position: relative;
    width: 100%; max-width: 520px;
    margin-top: 44px;
  }
  .url-box {
    display: flex; align-items: center;
    background: rgba(255,255,255,.06);
    border: .5px solid rgba(255,255,255,.16);
    border-radius: 16px;
    padding: 5px 5px 5px 18px;
    transition: border-color .2s, background .2s;
  }
  .url-box:focus-within {
    background: rgba(255,255,255,.08);
    border-color: rgba(255,255,255,.32);
  }
  .url-in {
    flex: 1; background: transparent; border: none; outline: none;
    color: #fff; font-size: 15px; letter-spacing: -.01em;
    min-width: 0;
  }
  .url-in::placeholder { color: rgba(255,255,255,.25); }
  .url-btn {
    background: #fff; color: #000;
    font-size: 14px; font-weight: 600; letter-spacing: -.01em;
    padding: 10px 22px; border-radius: 12px;
    border: none; cursor: pointer; flex-shrink: 0;
    transition: opacity .15s, transform .1s;
  }
  .url-btn:hover { opacity: .88; }
  .url-btn:active { transform: scale(.97); }
  .url-hint { margin-top: 12px; font-size: 12px; color: rgba(255,255,255,.2); }

  /* ── Video showcase ── */
  .showcase {
    padding: 0 24px 120px;
    display: flex; flex-direction: column; align-items: center;
  }
  .phones-row {
    display: flex; gap: 20px; align-items: flex-end; justify-content: center;
    flex-wrap: wrap;
  }

  /* Phone frame */
  .phone {
    flex: 0 0 auto;
    position: relative;
    display: flex; flex-direction: column; align-items: center; gap: 16px;
  }
  .phone.featured { transform: translateY(-20px); }

  .phone-shell {
    position: relative;
    border-radius: 38px;
    overflow: hidden;
    background: #0a0a0a;
    /* Dynamic Island border */
    outline: 1.5px solid rgba(255,255,255,.12);
    outline-offset: -1px;
  }
  .phone-shell.sm { width: 188px; height: 408px; border-radius: 34px; }
  .phone-shell.lg { width: 220px; height: 477px; border-radius: 38px; }

  /* Shadow */
  .phone-shell.sm { box-shadow: 0 32px 64px rgba(0,0,0,.7), 0 0 0 .5px rgba(255,255,255,.05) inset; }
  .phone-shell.lg { box-shadow: 0 48px 80px rgba(0,0,0,.8), 0 0 50px rgba(120,80,255,.12), 0 0 0 .5px rgba(255,255,255,.06) inset; }

  /* Dynamic Island */
  .phone-shell::before {
    content: '';
    position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
    width: 90px; height: 26px;
    background: #000; border-radius: 14px;
    z-index: 10;
  }
  .phone-shell.sm::before { width: 80px; height: 22px; border-radius: 12px; top: 10px; }

  /* Gloss */
  .phone-shell::after {
    content: '';
    position: absolute; inset: 0;
    background: linear-gradient(145deg, rgba(255,255,255,.05) 0%, transparent 45%);
    pointer-events: none; z-index: 5;
  }

  .phone-video {
    width: 100%; height: 100%;
    object-fit: cover; display: block;
  }
  .phone-label {
    font-size: 12px; color: rgba(255,255,255,.35);
    font-weight: 500; text-align: center;
    letter-spacing: -.01em; line-height: 1.4;
  }

  /* ── Divider ── */
  .divider { border: none; border-top: .5px solid rgba(255,255,255,.07); margin: 0; }

  /* ── Steps ── */
  .steps-section {
    padding: 100px 24px;
    max-width: 960px; margin: 0 auto;
  }
  .steps-grid {
    display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; margin-top: 60px;
  }
  @media (max-width: 640px) { .steps-grid { grid-template-columns: 1fr; } }

  .step-card {
    background: rgba(255,255,255,.03);
    border: .5px solid rgba(255,255,255,.08);
    border-radius: 20px;
    padding: 28px 24px 28px;
    transition: background .2s, border-color .2s;
  }
  .step-card:hover { background: rgba(255,255,255,.05); border-color: rgba(255,255,255,.13); }
  .step-num { font-size: 11px; font-weight: 600; letter-spacing: .1em; color: rgba(255,255,255,.2); margin-bottom: 20px; text-transform: uppercase; }
  .step-icon-wrap {
    width: 44px; height: 44px; border-radius: 12px;
    background: rgba(255,255,255,.06); border: .5px solid rgba(255,255,255,.1);
    display: flex; align-items: center; justify-content: center;
    margin-bottom: 18px;
  }
  .step-title { font-size: 16px; font-weight: 600; letter-spacing: -.02em; margin-bottom: 8px; }
  .step-desc { font-size: 13px; color: rgba(255,255,255,.45); line-height: 1.65; }

  /* ── Bottom CTA ── */
  .cta-section {
    padding: 120px 24px;
    text-align: center;
    max-width: 640px; margin: 0 auto;
  }
  .cta-section .t-headline { font-size: clamp(36px, 5vw, 56px); }

  /* ── Footer ── */
  footer {
    border-top: .5px solid rgba(255,255,255,.07);
    padding: 28px 32px;
    display: flex; align-items: center; justify-content: space-between;
    font-size: 12px; color: rgba(255,255,255,.2);
  }
  footer a { color: inherit; text-decoration: none; }
  footer a:hover { color: rgba(255,255,255,.5); }

  /* ── Animations ── */
  .reveal {
    opacity: 0; transform: translateY(20px);
    transition: opacity .8s cubic-bezier(.16,1,.3,1), transform .8s cubic-bezier(.16,1,.3,1);
  }
  .reveal.in { opacity: 1; transform: translateY(0); }
  .reveal-d1 { transition-delay: .08s; }
  .reveal-d2 { transition-delay: .16s; }
</style>
</head>
<body>

<!-- Nav -->
<nav>
  <a href="/" class="nav-logo">
    <span class="w-ad">Ad</span><span class="w-reel">Reel</span>
  </a>
  <nav class="nav-links">
    <a href="#how" class="nav-link">How it works</a>
    <a href="/app" class="nav-link">App</a>
  </nav>
  <a href="/app" class="nav-cta">Try free</a>
</nav>

<!-- Hero -->
<section class="hero">
  <div class="hero-bg"></div>

  <h1 class="t-headline g-white reveal" style="max-width:780px">
    Your product.<br>A scroll-stopping ad.
  </h1>

  <p class="t-subhead reveal reveal-d1" style="max-width:480px; margin-top:20px">
    Paste a link. Get a cinematic ad.<br>No crew. No timeline. Just results.
  </p>

  <div class="url-wrap reveal reveal-d2">
    <div class="url-box">
      <svg style="flex-shrink:0;margin-right:10px;opacity:.35" width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M6.5 1.5A5 5 0 0 1 11.5 6.5a5 5 0 0 1-1.09 3.09L14 13.17 13.17 14l-3.58-3.59A5 5 0 0 1 6.5 11.5a5 5 0 0 1-5-5 5 5 0 0 1 5-5z" fill="white"/>
      </svg>
      <input class="url-in" id="hero-url" type="url"
        placeholder="Paste a product URL to get started…"
        autocomplete="off" spellcheck="false"/>
      <button class="url-btn" onclick="handleGenerate()">Generate</button>
    </div>
    <p class="url-hint">No credit card required · First video free</p>
  </div>
</section>

<!-- Video showcase -->
<section class="showcase">
  <p class="t-label reveal" style="margin-bottom:52px">Made with AdReel</p>
  <div class="phones-row">

    <div class="phone reveal">
      <div class="phone-shell sm">
        <video class="phone-video" src="https://storage.googleapis.com/adreel-demo-videos/demo_butterfly.mp4"
          autoplay muted loop playsinline></video>
      </div>
      <p class="phone-label">Butterfly hair clip</p>
    </div>

    <div class="phone featured reveal reveal-d1">
      <div class="phone-shell lg">
        <video class="phone-video" src="https://storage.googleapis.com/adreel-demo-videos/demo_face_cream.mp4"
          autoplay muted loop playsinline></video>
      </div>
      <p class="phone-label">Face cream launch</p>
    </div>

    <div class="phone reveal reveal-d2">
      <div class="phone-shell sm">
        <video class="phone-video" src="https://storage.googleapis.com/adreel-demo-videos/demo_nike.mp4"
          autoplay muted loop playsinline></video>
      </div>
      <p class="phone-label">Nike Metcon 10</p>
    </div>

  </div>
</section>

<hr class="divider">

<!-- How it works -->
<section id="how" style="scroll-margin-top:72px">
  <div class="steps-section">
    <p class="t-label reveal" style="text-align:center">How it works</p>
    <h2 class="t-headline g-white reveal reveal-d1" style="text-align:center;margin-top:12px;font-size:clamp(36px,5vw,52px)">
      Three steps.<br>Zero editing skills.
    </h2>
    <div class="steps-grid">
      <div class="step-card reveal">
        <p class="step-num">01</p>
        <div class="step-icon-wrap">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" opacity=".75">
            <path d="M8.5 4.5H5A3.5 3.5 0 0 0 5 11.5h2M11.5 15.5H15A3.5 3.5 0 0 0 15 8.5h-2M7 10h6" stroke="white" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
        </div>
        <p class="step-title">Describe your product</p>
        <p class="step-desc">Paste a URL or describe your product. Upload a photo for best results.</p>
      </div>
      <div class="step-card reveal reveal-d1">
        <p class="step-num">02</p>
        <div class="step-icon-wrap">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" opacity=".75">
            <path d="M11 2L4 11h6l-1 7 7-9h-6l1-7z" stroke="white" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
          </svg>
        </div>
        <p class="step-title">AI writes and shoots</p>
        <p class="step-desc">The agent plans the storyboard, generates cinematic shots with your exact product, adds music.</p>
      </div>
      <div class="step-card reveal reveal-d2">
        <p class="step-num">03</p>
        <div class="step-icon-wrap">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" opacity=".75">
            <rect x="5" y="2" width="10" height="16" rx="2.5" stroke="white" stroke-width="1.5"/>
            <circle cx="10" cy="15.5" r=".8" fill="white"/>
            <path d="M8 5.5h4" stroke="white" stroke-width="1.2" stroke-linecap="round"/>
          </svg>
        </div>
        <p class="step-title">Export and post</p>
        <p class="step-desc">Download a ready-to-post 9:16 MP4. Refine any scene with one message.</p>
      </div>
    </div>
  </div>
</section>

<hr class="divider">

<!-- Bottom CTA -->
<section>
  <div class="cta-section">
    <h2 class="t-headline g-white reveal" style="font-size:clamp(36px,5vw,56px)">
      Start your first ad.
    </h2>
    <p class="t-subhead reveal reveal-d1" style="margin-top:16px;font-size:18px">
      No setup. No timeline. No camera crew.
    </p>
    <div class="url-wrap reveal reveal-d2" style="margin:40px auto 0;max-width:460px">
      <div class="url-box">
        <input class="url-in" id="bottom-url" type="url"
          placeholder="https://your-product-url.com"
          autocomplete="off" spellcheck="false"/>
        <button class="url-btn" onclick="handleGenerateBottom()">Generate</button>
      </div>
    </div>
  </div>
</section>

<!-- Footer -->
<footer>
  <div style="display:flex;align-items:center;gap:0;font-size:13px;letter-spacing:-.03em">
    <span style="font-weight:300;color:rgba(255,255,255,.2)">Ad</span><span style="font-weight:700;color:rgba(255,255,255,.2)">Reel</span>
    <span style="margin-left:8px;color:rgba(255,255,255,.15)">· © 2026</span>
  </div>
  <div style="display:flex;gap:20px">
    <a href="/app">Open App</a>
    <a href="https://github.com/ejzhu2025/adreel" target="_blank">GitHub</a>
  </div>
</footer>

<script>
  function goToApp(url) {
    window.location.href = url ? '/app?url=' + encodeURIComponent(url) : '/app';
  }
  function handleGenerate() {
    goToApp(document.getElementById('hero-url').value.trim());
  }
  function handleGenerateBottom() {
    goToApp(document.getElementById('bottom-url').value.trim());
  }
  document.getElementById('hero-url').addEventListener('keydown', e => { if (e.key==='Enter') handleGenerate(); });
  document.getElementById('bottom-url').addEventListener('keydown', e => { if (e.key==='Enter') handleGenerateBottom(); });

  // Scroll reveal
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => { if (e.isIntersecting) e.target.classList.add('in'); });
  }, { threshold: 0.1 });
  document.querySelectorAll('.reveal').forEach(el => io.observe(el));
</script>
</body>
</html>
"""
