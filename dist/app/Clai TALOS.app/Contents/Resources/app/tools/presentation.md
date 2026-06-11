# Presentations

Create single-file HTML slide presentations and serve them via the project gateway.

Default expectation: every deck should feel premium, expressive, and custom-designed, not a generic template.

## How to Create a Presentation

1. Call `create_project(name="topic-deck", html=<full_html>, description="Presentation about topic")`
2. Send the user the `url` from the response — that's the live link. One step, done.

## HTML Structure

Every presentation is a single self-contained `index.html` - inline CSS, inline JS, no external dependencies except Google Fonts.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Presentation Title</title>
  <link href="https://fonts.googleapis.com/css2?family=FONT+NAME:wght@300;400;600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
    :root {
      --bg-1: #08111f;
      --bg-2: #10233f;
      --ink: #f6f7fb;
      --muted: #b8c2d9;
      --accent: #50e3c2;
      --accent-2: #ffbf69;
      --glass: rgba(255,255,255,0.12);
      --shadow: 0 20px 60px rgba(0,0,0,0.35);
    }
    html, body { width: 100%; height: 100%; overflow: hidden; font-family: 'FONT', sans-serif; background: radial-gradient(1200px 700px at 10% 10%, #163056 0%, transparent 60%), linear-gradient(140deg, var(--bg-1), var(--bg-2)); color: var(--ink); }
    .slides { width: 100%; height: 100vh; position: relative; }
    .slide {
      position: absolute; inset: 0;
      display: flex; flex-direction: column; justify-content: center; align-items: center;
      padding: 5vh 8vw; opacity: 0; visibility: hidden;
      transition: opacity 0.6s ease, visibility 0.6s ease;
      isolation: isolate;
    }
    .slide.active { opacity: 1; visibility: visible; }

    .slide::before,
    .slide::after {
      content: "";
      position: absolute;
      border-radius: 999px;
      filter: blur(40px);
      z-index: -1;
      opacity: 0;
      transform: scale(0.9);
      transition: opacity 1.1s ease, transform 1.1s ease;
    }
    .slide::before { width: 26vw; height: 26vw; top: 10%; left: -8%; background: color-mix(in srgb, var(--accent) 70%, transparent); }
    .slide::after { width: 28vw; height: 28vw; right: -8%; bottom: -10%; background: color-mix(in srgb, var(--accent-2) 60%, transparent); }
    .slide.active::before,
    .slide.active::after { opacity: 0.5; transform: scale(1); }

    /* Staggered entrance animations */
    .el { opacity: 0; transform: translateY(30px); transition: opacity 0.7s ease, transform 0.7s ease; }
    .slide.active .el { opacity: 1; transform: translateY(0); }
    .slide.active .d1 { transition-delay: 0.1s; }
    .slide.active .d2 { transition-delay: 0.2s; }
    .slide.active .d3 { transition-delay: 0.3s; }
    .slide.active .d4 { transition-delay: 0.4s; }
    .slide.active .d5 { transition-delay: 0.5s; }
    .slide.active .d6 { transition-delay: 0.6s; }

    /* Animation variants */
    .el-left { transform: translateX(-40px); }
    .slide.active .el-left { transform: translateX(0); }
    .el-scale { transform: scale(0.8); }
    .slide.active .el-scale { transform: scale(1); }
    .el-fade { transform: none; }
    .el-rotate { transform: rotate(-3deg) scale(0.95); }
    .slide.active .el-rotate { transform: rotate(0deg) scale(1); }

    /* Card and icon primitives */
    .card {
      background: linear-gradient(145deg, rgba(255,255,255,0.12), rgba(255,255,255,0.05));
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
      padding: 1.1rem 1.2rem;
    }
    .icon-chip {
      width: 42px;
      height: 42px;
      border-radius: 12px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(145deg, var(--accent), color-mix(in srgb, var(--accent) 45%, #fff));
      color: #04222a;
      font-size: 1.1rem;
      font-weight: 700;
      box-shadow: 0 10px 24px rgba(0,0,0,0.24);
    }

    /* Navigation */
    .nav {
      position: fixed;
      bottom: 2.2vh;
      left: 50%;
      transform: translateX(-50%);
      display: flex;
      gap: 9px;
      z-index: 100;
      padding: 7px 11px;
      border-radius: 999px;
      background: rgba(8, 13, 23, 0.55);
      border: 1px solid rgba(255,255,255,0.15);
      backdrop-filter: blur(10px);
    }
    .dot {
      min-width: 26px;
      height: 26px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.22);
      color: rgba(255,255,255,0.72);
      font-size: 0.72rem;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: all 0.28s cubic-bezier(.2,.8,.2,1);
      background: linear-gradient(180deg, rgba(255,255,255,0.16), rgba(255,255,255,0.05));
    }
    .dot.active {
      color: #06131f;
      border-color: transparent;
      background: linear-gradient(130deg, var(--accent), var(--accent-2));
      transform: translateY(-2px) scale(1.04);
      box-shadow: 0 8px 24px rgba(0,0,0,0.35);
    }
    .dot:hover { transform: translateY(-1px); }
    .arrows { position: fixed; top: 50%; width: 100%; display: flex; justify-content: space-between; padding: 0 2vw; z-index: 100; pointer-events: none; }
    .arrow {
      pointer-events: all;
      cursor: pointer;
      font-size: 1.1rem;
      color: rgba(255,255,255,0.82);
      width: 40px;
      height: 40px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.18);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: rgba(8, 13, 23, 0.46);
      backdrop-filter: blur(8px);
      transition: transform 0.2s ease, background 0.2s ease;
      user-select: none;
    }
    .arrow:hover { transform: scale(1.06); background: rgba(8, 13, 23, 0.75); }
    .counter {
      position: fixed;
      top: 2vh;
      right: 2vw;
      z-index: 100;
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: rgba(255,255,255,0.85);
      padding: 0.4rem 0.65rem;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.18);
      background: rgba(8, 13, 23, 0.52);
      backdrop-filter: blur(8px);
    }
    .counter .current { color: var(--accent-2); font-weight: 700; }

    /* Responsive */
    @media (max-width: 768px) {
      .slide { padding: 4vh 6vw; }
      h1 { font-size: clamp(1.8rem, 6vw, 3rem) !important; }
      .nav { gap: 6px; padding: 5px 8px; }
      .dot { min-width: 22px; height: 22px; font-size: 0.62rem; }
      .counter { top: 1.2vh; right: 3vw; }
      .arrows { padding: 0 3vw; }
      .arrow { width: 34px; height: 34px; }
    }
    @media (min-width: 1400px) { .slide { padding: 5vh 8vw; } }
    @media (min-width: 1920px) { .slide { padding: 5vh 10vw; } }

    @keyframes floaty {
      0%, 100% { transform: translateY(0px); }
      50% { transform: translateY(-8px); }
    }
    .floaty { animation: floaty 5.2s ease-in-out infinite; }
  </style>
</head>
<body>
  <div class="slides">
    <div class="slide active" style="background: linear-gradient(135deg, #0a0a2e, #1a1a4e);">
      <h1 class="el d1" style="font-size: clamp(2.5rem, 6vw, 5rem); color: white;">Title</h1>
      <p class="el d2" style="color: rgba(255,255,255,0.7); margin-top: 1rem;">Subtitle</p>
    </div>
    <!-- More slides... -->
    <div class="slide" style="background: linear-gradient(135deg, #1a1a2e, #16213e);">
      <h2 class="el d1" style="font-size: 3rem; color: white;">Thank You</h2>
      <p class="el d2" style="color: rgba(255,255,255,0.6); margin-top: 1rem;">Questions & Discussion</p>
    </div>
    <div class="slide" style="background: #0a0a0a;">
      <h2 class="el d1" style="font-size: 1.5rem; color: white; margin-bottom: 2rem;">Sources</h2>
      <div class="el d2" style="color: rgba(255,255,255,0.6); font-size: 0.85rem; line-height: 1.8;">
        <p>Source 1 — url.com</p>
      </div>
    </div>
  </div>
  <div class="counter"><span class="current">01</span> / <span class="total"></span></div>
  <div class="arrows"><div class="arrow" onclick="prev()">&#8592;</div><div class="arrow" onclick="next()">&#8594;</div></div>
  <div class="nav" id="nav"></div>
  <script>
    const slides=document.querySelectorAll('.slide');let current=0;const total=slides.length;
    document.querySelector('.total').textContent=String(total).padStart(2,'0');
    const nav=document.getElementById('nav');
    slides.forEach((_,i)=>{const d=document.createElement('button');d.className='dot'+(i===0?' active':'');d.type='button';d.setAttribute('aria-label',`Go to slide ${i+1}`);d.textContent=String(i+1).padStart(2,'0');d.onclick=()=>goTo(i);nav.appendChild(d);});
    function goTo(i){slides[current].classList.remove('active');nav.children[current].classList.remove('active');current=Math.max(0,Math.min(i,total-1));slides[current].classList.add('active');nav.children[current].classList.add('active');document.querySelector('.current').textContent=String(current+1).padStart(2,'0');}
    function next(){goTo(current+1);}function prev(){goTo(current-1);}
    document.addEventListener('keydown',e=>{if(['ArrowDown','ArrowRight',' '].includes(e.key)){e.preventDefault();next();}if(['ArrowUp','ArrowLeft'].includes(e.key)){e.preventDefault();prev();}});
    let ty=0;document.addEventListener('touchstart',e=>ty=e.touches[0].clientY);
    document.addEventListener('touchend',e=>{const d=ty-e.changedTouches[0].clientY;if(Math.abs(d)>50){d>0?next():prev();}});
  </script>
</body>
</html>
```

## Design Rules

**No two presentations should look similar.** Vary fonts, colors, layouts, and animations every time.

## Default Quality Baseline (Always Apply)

Every generated presentation should include all of the following by default:

1. A clear visual theme system with CSS variables (backgrounds, text, accents, card surfaces, glow/shadow).
2. A distinctive font pairing from Google Fonts (display + body), matched to subject matter.
3. At least 3 layout patterns across the deck (hero, split, asymmetric grid, timeline, metric wall, quote stage).
4. Rich motion language: staggered reveals plus at least 2 additional animation variants (slide, scale, rotate, parallax, float).
5. Decorative but purposeful visual elements (glows, gradients, abstract shapes, textured overlays).
6. Iconography on insight cards and key bullets (emoji is acceptable when icon packs are unavailable).
7. Stylized slide number indicators (pill/stepper/chips), not plain unlabeled dots.
8. Distinctive slide counter treatment with zero-padded numbering (`01 / 08`) and theme styling.
9. Strong contrast and mobile-safe spacing so content remains readable on small screens.

If the user gives no style preference, choose a bold theme that fits the topic instead of a neutral default.

| Subject | Fonts | Colors | Vibe |
|---------|-------|--------|------|
| Historical/Biography | Serif (Playfair Display, Cormorant Garamond) + clean sans body | Warm amber, parchment, bronze | Documentary |
| Tech/Startup | Space Grotesk, Sora, Manrope | Electric cyan, deep navy, neon accents | Futuristic |
| Academic/Educational | Source Serif + Source Sans / Lora + Inter | Slate blue, white, subtle gold accent | Authoritative |
| Nature/Environmental | DM Serif Text + Nunito Sans | Moss, forest, sky, sand | Organic |
| Creative/Portfolio | Bebas Neue, Syne, Bricolage Grotesque | Saturated contrasting hues | Expressive |
| Business/Consulting | Plus Jakarta Sans, IBM Plex Sans | Navy, steel, emerald/coral accent | Polished |

Avoid defaulting to Inter + plain dark gradients unless the user explicitly asks for minimalist style.

## Slide Types

- **Opening** — Centered, bold, minimal
- **Context** — Label + headline + narrative body
- **Split** — Two columns (problem/solution, before/after)
- **Impact** — One huge number or word, small context below
- **Quote** — Large italicized text with attribution
- **Timeline** — Vertical line with dates
- **Stats** — Numbers with labels in cards or grid
- **Closing** — Reflective statement, call to action

## Navigation and Indicator Rules

- Use stylized numbered chips/steps for slide navigation, not anonymous tiny circles.
- Active indicator must have clear emphasis (color fill, glow, or elevation shift).
- Keep navigation visible on desktop and mobile, with touch-friendly sizes.
- Include keyboard navigation (arrow keys + space) and swipe support.

## Required Final Slides

Every presentation MUST end with:
1. **Thank You** slide (second to last)
2. **Citations/Sources** slide (always last)

## Responsive Font Scaling

```css
h1 { font-size: clamp(2rem, 5vw, 4.5rem); }
h2 { font-size: clamp(1.5rem, 3.5vw, 3rem); }
p  { font-size: clamp(0.9rem, 1.2vw, 1.1rem); }
```

## Images

Use Unsplash for free images: `https://images.unsplash.com/photo-ID?w=1200`

For local images, download them into the project directory and reference with relative paths:
```bash
# Download image
curl -L -o <project_path>/images/photo.jpg "IMAGE_URL"
```

```html
<!-- Background image with overlay -->
<div class="slide" style="background: linear-gradient(to bottom, rgba(0,0,0,0.5), #000), url('images/photo.jpg') center/cover;">

<!-- Inline image -->
<img src="images/photo.jpg" style="max-width: 80%; border-radius: 8px;">
```
