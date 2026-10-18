# Presentations

Create single-file HTML slide presentations and serve them via the project gateway.

Default expectation: every deck should feel premium, expressive, and custom-designed, not a generic template — and it should be VISUAL: charts, diagrams, images, and real motion, not walls of text.

## How to Create a Presentation

1. Call `create_project(name="topic-deck", html=<full_html>, description="Presentation about topic")`
2. Send the user the `url` from the response — that's the live link. One step, done.

## HTML Structure

Every presentation is a single self-contained `index.html` - inline CSS, inline JS, no external dependencies except Google Fonts. All charts and diagrams are pure CSS/SVG (no chart libraries). Images come from Unsplash or the project directory.

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
    /* 3D entrances: tilt in from depth */
    .slide { perspective: 1200px; }
    .el-3d { transform: rotateX(-18deg) translateY(60px) scale(0.92); transform-style: preserve-3d; }
    .slide.active .el-3d { transform: rotateX(0deg) translateY(0) scale(1); }

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
      width: 42px; height: 42px; border-radius: 12px;
      display: inline-flex; align-items: center; justify-content: center;
      background: linear-gradient(145deg, var(--accent), color-mix(in srgb, var(--accent) 45%, #fff));
      color: #04222a; font-size: 1.1rem; font-weight: 700;
      box-shadow: 0 10px 24px rgba(0,0,0,0.24);
    }

    /* Navigation */
    .nav {
      position: fixed; bottom: 2.2vh; left: 50%; transform: translateX(-50%);
      display: flex; gap: 9px; z-index: 100; padding: 7px 11px;
      border-radius: 999px; background: rgba(8, 13, 23, 0.55);
      border: 1px solid rgba(255,255,255,0.15); backdrop-filter: blur(10px);
    }
    .dot {
      min-width: 26px; height: 26px; border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.22); color: rgba(255,255,255,0.72);
      font-size: 0.72rem; font-weight: 700;
      display: inline-flex; align-items: center; justify-content: center;
      cursor: pointer; transition: all 0.28s cubic-bezier(.2,.8,.2,1);
      background: linear-gradient(180deg, rgba(255,255,255,0.16), rgba(255,255,255,0.05));
    }
    .dot.active {
      color: #06131f; border-color: transparent;
      background: linear-gradient(130deg, var(--accent), var(--accent-2));
      transform: translateY(-2px) scale(1.04);
      box-shadow: 0 8px 24px rgba(0,0,0,0.35);
    }
    .dot:hover { transform: translateY(-1px); }
    .arrows { position: fixed; top: 50%; width: 100%; display: flex; justify-content: space-between; padding: 0 2vw; z-index: 100; pointer-events: none; }
    .arrow {
      pointer-events: all; cursor: pointer; font-size: 1.1rem;
      color: rgba(255,255,255,0.82); width: 40px; height: 40px; border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.18); display: inline-flex; align-items: center; justify-content: center;
      background: rgba(8, 13, 23, 0.46); backdrop-filter: blur(8px);
      transition: transform 0.2s ease, background 0.2s ease; user-select: none;
    }
    .arrow:hover { transform: scale(1.06); background: rgba(8, 13, 23, 0.75); }
    .counter {
      position: fixed; top: 2vh; right: 2vw; z-index: 100;
      font-size: 0.78rem; letter-spacing: 0.08em; text-transform: uppercase;
      color: rgba(255,255,255,0.85); padding: 0.4rem 0.65rem; border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.18); background: rgba(8, 13, 23, 0.52);
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

    @keyframes floaty { 0%, 100% { transform: translateY(0px); } 50% { transform: translateY(-8px); } }
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

    // Object-matched transitions: the browser animates shared elements
    // (logo, hero number, avatar) smoothly from slide to slide.
    const vtSupported = 'startViewTransition' in document;
    function goTo(i){
      const apply = () => {
        slides[current].classList.remove('active');nav.children[current].classList.remove('active');
        current=Math.max(0,Math.min(i,total-1));
        slides[current].classList.add('active');nav.children[current].classList.add('active');
        document.querySelector('.current').textContent=String(current+1).padStart(2,'0');
        runCharts(slides[current]);
      };
      if (vtSupported) { document.startViewTransition(apply); } else { apply(); }
    }
    function next(){goTo(current+1);}function prev(){goTo(current-1);}
    document.addEventListener('keydown',e=>{if(['ArrowDown','ArrowRight',' '].includes(e.key)){e.preventDefault();next();}if(['ArrowUp','ArrowLeft'].includes(e.key)){e.preventDefault();prev();}});
    let ty=0;document.addEventListener('touchstart',e=>ty=e.touches[0].clientY);
    document.addEventListener('touchend',e=>{const d=ty-e.changedTouches[0].clientY;if(Math.abs(d)>50){d>0?next():prev();}});
    runCharts(slides[0]); // animate charts on first slide too
  </script>
</body>
</html>
```

## Design Rules

**No two presentations should look similar.** Vary fonts, colors, layouts, and animations every time. Pick a DESIGN LANGUAGE (below) that fits the subject and commit to it across the whole deck.

## Design Languages (choose one per deck, fully commit)

| Language | Fonts | Palette & Texture | Motion Feel |
|----------|-------|-------------------|-------------|
| Editorial / Magazine | Playfair Display or Fraunces + Inter | Paper white/cream, ink black, one red accent; hairline rules, drop caps, big margins | Slow elegant fades, gentle rises, typewriter reveals |
| Neo-Brutalist | Archivo Black / Space Mono | Flat saturated yellow/pink/cyan on off-white, thick 3px black borders, hard offset shadows, no blur | Snappy: instant pops, hard slides, stamp-in rotations |
| Art Deco | Poiret One / Cinzel + Jost | Midnight navy + gold, geometric fan/sunburst patterns, thin gold frames | Symmetric wipes, shimmering gold glints, ornamental dividers |
| Terminal / Hacker | JetBrains Mono / VT323 | Near-black, phosphor green or amber, scanline overlay, monospace grid | CRT flicker, blinking cursors, char-by-char typing |
| Glassmorphism | Sora + Manrope | Deep gradient behind frosted translucent cards, vivid accent glows | Floaty parallax, blur pulses, soft 3D tilts |
| Swiss / Minimal | Neue Haas via Inter Tight + Helvetica-like | White, black, one signal red; giant numbers, strict grid, lots of whitespace | Precise: grid-aligned slides, counting rules, zero flourish |
| Organic / Nature | DM Serif Display + Nunito Sans | Moss, clay, sand, sky; soft grain texture, rounded corners, leaf motifs | Slow growth animations (scale from root), drifting particles |
| Cyber-Neon | Orbitron / Michroma + Rajdhani | Black/violet with cyan and magenta neon glows, grid horizons, glitch accents | Glitch slides, neon flicker, perspective grid motion |
| Hand-drawn / Scrapbook | Caveat / Patrick Hand + Karla | Paper texture, tape strips, polaroid frames, marker underlines, tilted cards | Playful rotations, wobble bounces, polaroid drops |
| Corporate Bold | IBM Plex Sans + IBM Plex Mono | Steel blue, white, emerald accent; bento grids, precise data tables | Confident metric reveals, count-ups, chart draws |

If the user gives no style preference, pick the language that fits the TOPIC's tone (startup → Glassmorphism or Corporate Bold; history → Editorial; security/hacker culture → Terminal) — never fall back to "generic dark gradient".

## Visual Content — MANDATORY, Not Optional

A deck of only text slides is a failed deck. Every substantial deck must mix in **charts, diagrams, and images**:

- **At least one chart** on any slide with numbers, comparisons, or trends
- **At least one diagram** (flow, timeline, architecture, or relationship) per deck
- **Images** for narrative or hero slides (Unsplash or downloaded)
- **Animated numbers** for metric slides — never a static "73%"

### Charts — pure CSS/SVG, no libraries

**Animated bar chart** (bars grow from the bottom when the slide activates):
```html
<div class="chart-bars" style="display:flex; align-items:flex-end; gap:14px; height:260px;">
  <div class="bar-wrap el d1" style="flex:1; display:flex; flex-direction:column; align-items:center; gap:8px;">
    <div class="bar" data-h="82" style="width:100%; height:0%; background:linear-gradient(180deg, var(--accent), color-mix(in srgb, var(--accent) 40%, transparent)); border-radius:8px 8px 0 0; transition:height 1s cubic-bezier(.2,.8,.2,1);"></div>
    <span style="font-size:0.72rem; color:var(--muted);">2023</span>
  </div>
  <!-- more bar-wraps with data-h percentages -->
</div>
<script>
// runCharts(): animate every .bar in the active slide to its data-h height
function runCharts(scope){
  (scope||document).querySelectorAll('.bar').forEach((b,i)=>{
    b.style.height='0%';
    setTimeout(()=>{ b.style.height=b.dataset.h+'%'; }, 150+i*120);
  });
  (scope||document).querySelectorAll('.count-up').forEach(el=>countUp(el));
  (scope||document).querySelectorAll('svg .draw').forEach(path=>{
    const len=path.getTotalLength();
    path.style.strokeDasharray=len; path.style.strokeDashoffset=len;
    path.getBoundingClientRect(); // force layout
    path.style.transition='stroke-dashoffset 1.4s cubic-bezier(.4,0,.2,1)';
    path.style.strokeDashoffset='0';
  });
}
</script>
```

**Line chart with draw-on animation** (SVG path draws itself):
```html
<svg viewBox="0 0 600 260" style="width:min(90%,720px);">
  <polyline class="draw" points="20,220 110,180 200,195 290,120 380,140 470,70 560,55"
    fill="none" stroke="var(--accent)" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  <polyline class="draw" points="20,230 110,225 200,205 290,210 380,170 470,150 560,130"
    fill="none" stroke="var(--accent-2)" stroke-width="3" stroke-dasharray="1 0"/>
  <!-- grid lines: light horizontal lines + axis labels -->
</svg>
```
(For polylines, set `stroke-dasharray: none` in CSS and let runCharts animate a dash cover — or use a `<path>` with a single `d` so `getTotalLength` works.)

**Donut / progress ring:**
```html
<svg viewBox="0 0 120 120" style="width:160px;">
  <circle cx="60" cy="60" r="52" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="12"/>
  <circle cx="60" cy="60" r="52" fill="none" stroke="var(--accent)" stroke-width="12"
    stroke-linecap="round" class="donut" data-pct="73"
    stroke-dasharray="326.7" stroke-dashoffset="326.7"
    transform="rotate(-90 60 60)" style="transition:stroke-dashoffset 1.2s cubic-bezier(.2,.8,.2,1);"/>
  <text x="60" y="58" text-anchor="middle" class="count-up" data-to="73" style="fill:var(--ink); font-size:26px; font-weight:700;">0</text>
  <text x="60" y="76" text-anchor="middle" style="fill:var(--muted); font-size:10px; letter-spacing:0.1em;">ADOPTED</text>
</svg>
<script>
// in runCharts(): document.querySelectorAll('.donut').forEach(d=>{
//   const C=2*Math.PI*52; d.style.strokeDashoffset=C*(1-(+d.dataset.pct)/100); });
</script>
```

**Animated counters** (numbers count up):
```html
<span class="count-up" data-to="34000" data-suffix="+" data-dur="1400">0</span>
<script>
function countUp(el){
  const to=+el.dataset.to||0, dur=+el.dataset.dur||1200, suffix=el.dataset.suffix||'';
  const t0=performance.now();
  function frame(t){
    const p=Math.min(1,(t-t0)/dur), eased=1-Math.pow(1-p,3);
    el.textContent=Math.round(to*eased).toLocaleString()+suffix;
    if(p<1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}
</script>
```

### Diagrams

**Flow / process** (nodes with animated connectors):
```html
<div class="flow" style="display:flex; align-items:center; gap:0; flex-wrap:wrap; justify-content:center;">
  <div class="card el d1" style="min-width:150px; text-align:center;"><div class="icon-chip" style="margin:0 auto 8px;">1</div>Collect</div>
  <div class="flow-arrow el d2" style="width:56px; height:2px; background:linear-gradient(90deg, transparent, var(--accent)); position:relative; margin:0 6px;">&nbsp;</div>
  <!-- arrowheads via a small CSS triangle on ::after -->
</div>
```

**Timeline** (vertical line, events stagger in):
```html
<div class="timeline" style="position:relative; padding-left:34px;">
  <div style="position:absolute; left:10px; top:6px; bottom:6px; width:2px; background:linear-gradient(180deg, var(--accent), var(--accent-2));"></div>
  <div class="el d1" style="position:relative; margin-bottom:1.4rem;">
    <div style="position:absolute; left:-30px; top:4px; width:12px; height:12px; border-radius:50%; background:var(--accent); box-shadow:0 0 12px var(--accent);"></div>
    <strong>2023 Q1</strong> — what happened
  </div>
</div>
```

**Other diagram patterns**: bento grid (CSS grid of stat cards), venn (two overlapping translucent circles with `mix-blend-mode: screen`), org chart (flex columns + connector pseudo-elements), before/after slider (two stacked divs, clip-path animated), comparison table with row stagger, layered architecture stack (stacked translucent rectangles with 3D rotateX perspective showing "layers").

### Images

Use Unsplash for free images: `https://images.unsplash.com/photo-ID?w=1200`

For local images, download them into the project directory and reference with relative paths:
```bash
curl -L -o <project_path>/images/photo.jpg "IMAGE_URL"
```

```html
<!-- Background image with overlay -->
<div class="slide" style="background: linear-gradient(to bottom, rgba(0,0,0,0.5), #000), url('images/photo.jpg') center/cover;">

<!-- Inline image -->
<img src="images/photo.jpg" style="max-width: 80%; border-radius: 8px;">

<!-- Hero image with slow zoom (Ken Burns) -->
<img class="kenburns" src="images/photo.jpg" style="position:absolute; inset:0; width:100%; height:100%; object-fit:cover;">
<style>
@keyframes kb { from { transform: scale(1) translateY(0); } to { transform: scale(1.12) translateY(-2%); } }
.kenburns { animation: kb 14s ease-in-out forwards; }
</style>
```

Pick concrete Unsplash photo IDs that match the subject — nature, cities, technology, people at work. Never leave an image placeholder.

## Advanced Motion — Beyond Slide-in/Slide-out

Vary the motion language per deck. Use several of these in every presentation:

**3D transforms** (real depth, not just fades):
```css
.slide { perspective: 1200px; }
/* Tilt-in entrance */
.el-3d { opacity:0; transform: rotateX(-24deg) translateY(80px) scale(0.9); transform-style: preserve-3d; transition: all 0.8s cubic-bezier(.2,.8,.2,1); }
.slide.active .el-3d { opacity:1; transform: none; }

/* 3D flip card — flips on entrance */
.flip { transform-style: preserve-3d; }
.slide.active .flip { animation: flipIn 0.9s cubic-bezier(.2,.8,.2,1) both; }
@keyframes flipIn { from { transform: rotateY(-90deg); opacity: 0; } to { transform: rotateY(0); opacity: 1; } }

/* Fanned card deck */
.deck { transform-style: preserve-3d; }
.deck .card { position:absolute; transition: transform 0.6s cubic-bezier(.2,.8,.2,1); }
.deck .card:nth-child(1) { transform: rotate(-8deg) translateX(-70px); }
.deck .card:nth-child(2) { transform: rotate(-3deg) translateX(-24px); }
.slide.active .deck .card { transform: rotate(0deg) translateX(0); } /* then fan out on hover/next */
```

**Object-matched transitions** (shared elements glide between slides — the View Transitions API, already wired into the skeleton's `goTo`):
```css
/* Give a recurring element (logo, hero stat, avatar) the same name on
   every slide; the browser animates it flying to its new position/size. */
.hero-stat { view-transition-name: hero-stat; }
.brand-mark { view-transition-name: brand; }

/* Style the crossfade of everything else */
::view-transition-old(root) { animation: 200ms ease both fade-out; }
::view-transition-new(root) { animation: 350ms ease both fade-in; }
@keyframes fade-out { to { opacity: 0; transform: scale(0.98); } }
@keyframes fade-in { from { opacity: 0; transform: scale(1.02); } }
```
Supported in all modern Chromium browsers and Safari 18+; silently falls back to the normal transition elsewhere. Use it for: the deck's logo, a running metric that grows across slides, a chapter number that flies to a corner.

**SVG path drawing** — covered in Charts (`runCharts` animates any `svg .draw` path with stroke-dashoffset): flowchart connectors, underlines, icons, decorative flourishes all draw themselves in.

**Ambient / looping motion**:
```css
@keyframes drift { 0%,100% { transform: translate(0,0) rotate(0deg); } 50% { transform: translate(12px,-16px) rotate(6deg); } }
.drift { animation: drift 11s ease-in-out infinite; }
@keyframes shimmer { 0% { background-position: -200% 0; } 100% { background-position: 200% 0; } }
.shimmer-text { background: linear-gradient(90deg, var(--ink) 40%, var(--accent) 50%, var(--ink) 60%); background-size: 200% 100%; -webkit-background-clip: text; background-clip: text; color: transparent; animation: shimmer 3.2s linear infinite; }
@keyframes pulse-ring { 0% { box-shadow: 0 0 0 0 rgba(80,227,194,0.5); } 100% { box-shadow: 0 0 0 26px rgba(80,227,194,0); } }
.pulse-ring { animation: pulse-ring 2s cubic-bezier(0,.6,.4,1) infinite; }
```

**Parallax depth** (background layers move slower than foreground):
```html
<div class="slide">
  <div class="parallax-bg" data-depth="0.3" style="position:absolute; inset:0; z-index:0;"></div>
  <div class="parallax-fg" data-depth="1" style="position:relative; z-index:1;">content</div>
</div>
<script>
document.querySelectorAll('.slide').forEach(s=>{
  s.addEventListener('pointermove', e=>{
    const r=s.getBoundingClientRect();
    const x=(e.clientX-r.left)/r.width-0.5, y=(e.clientY-r.top)/r.height-0.5;
    s.querySelectorAll('[data-depth]').forEach(el=>{
      const d=+el.dataset.depth;
      el.style.transform=`translate(${x*-24*d}px, ${y*-24*d}px)`;
    });
  });
});
</script>
```

**Typewriter / scramble reveals** (Terminal language decks especially):
```css
.typing { overflow:hidden; white-space:nowrap; border-right:2px solid var(--accent); }
.slide.active .typing { animation: type 1.6s steps(28) 0.3s both, caret 0.8s step-end infinite; }
@keyframes type { from { width: 0; } to { width: 100%; } }
@keyframes caret { 50% { border-color: transparent; } }
```

## Default Quality Baseline (Always Apply)

Every generated presentation should include all of the following by default:

1. A clear visual theme system with CSS variables (backgrounds, text, accents, card surfaces, glow/shadow).
2. A distinctive font pairing from Google Fonts (display + body), matched to subject matter.
3. At least 3 layout patterns across the deck (hero, split, asymmetric grid, timeline, metric wall, quote stage).
4. Rich motion language: staggered reveals plus at least 2 additional animation variants (3D tilt, flip, draw-on, parallax, float, shimmer).
5. At least one chart, one diagram, and images where the narrative calls for them — numbers are ALWAYS animated (count-ups / drawing bars), never static.
6. Decorative but purposeful visual elements (glows, gradients, abstract shapes, textured overlays).
7. Iconography on insight cards and key bullets (emoji is acceptable when icon packs are unavailable).
8. Stylized slide number indicators (pill/stepper/chips), not plain unlabeled dots.
9. Distinctive slide counter treatment with zero-padded numbering (`01 / 08`) and theme styling.
10. Strong contrast and mobile-safe spacing so content remains readable on small screens.
11. Object-matched transition (`view-transition-name`) on at least one recurring element when the deck has a natural one (logo, running metric).

If the user gives no style preference, choose a bold design language that fits the topic instead of a neutral default.

## Slide Types

- **Opening** — Centered, bold, minimal
- **Context** — Label + headline + narrative body
- **Split** — Two columns (problem/solution, before/after)
- **Impact** — One huge number or word, small context below
- **Quote** — Large italicized text with attribution
- **Timeline** — Vertical line with dates
- **Stats** — Animated numbers with labels in cards or grid
- **Chart** — Bars / line / donut with draw-in animations
- **Diagram** — Flow, architecture, or relationship map
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
