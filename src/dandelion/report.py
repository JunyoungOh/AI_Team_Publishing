"""Generate a self-contained interactive HTML report from a DandelionTree."""
from __future__ import annotations

import html
import json
from datetime import datetime

from src.dandelion.schemas import DandelionTree


def generate_html_report(tree: DandelionTree) -> str:
    """Return a standalone HTML string with embedded SVG tree + interactive seed cards."""
    themes_json = json.dumps(
        [t.model_dump() for t in tree.themes], ensure_ascii=False,
    )
    seeds_json = json.dumps(
        [s.model_dump() for s in tree.seeds], ensure_ascii=False,
    )
    query_escaped = html.escape(tree.query)
    created = tree.created_at or datetime.now().strftime("%Y-%m-%d %H:%M")

    return _TEMPLATE.replace("{{QUERY}}", query_escaped).replace(
        "{{CREATED_AT}}", created,
    ).replace(
        "{{THEMES_JSON}}", themes_json,
    ).replace(
        "{{SEEDS_JSON}}", seeds_json,
    )


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>미래아이디어 — {{QUERY}}</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:#0D1117;color:#E6EDF3;font-family:'Segoe UI','Apple SD Gothic Neo',sans-serif;overflow:hidden;height:100vh}

/* ── Layout: full-screen app clone ── */
.dandelion-app{display:flex;flex-direction:column;height:100vh;overflow:hidden}

/* ── Header ── */
.dandelion-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;height:48px;
  background:rgba(13,17,23,0.95);border-bottom:1px solid rgba(255,255,255,0.06);flex-shrink:0}
.dandelion-header h1{font-size:1.05em;color:#CE93D8;margin:0;font-family:'Segoe UI','Apple SD Gothic Neo',sans-serif}
.dandelion-header .meta{font-size:0.68em;color:#484f58;margin-left:12px}

/* ── SVG Canvas (fills remaining space) ── */
.dandelion-canvas{flex:1;position:relative;overflow:hidden}
.dandelion-canvas svg{width:100%;height:100%}

/* ── Stems ── */
.dandelion-stem{fill:none;stroke-width:2;stroke-linecap:round}
.dandelion-stem-animate{animation:stemGrow 0.8s ease-out forwards}
@keyframes stemGrow{from{stroke-dashoffset:var(--stem-length)}to{stroke-dashoffset:0}}

/* ── Origin circle ── */
.dandelion-origin{fill:#E6EDF3;opacity:0;animation:fadeIn 0.3s ease-out forwards}
@keyframes fadeIn{to{opacity:1}}

/* ── Seeds ── */
.dandelion-seed{cursor:pointer;transform-origin:center;transition:r 0.2s ease,opacity 0.3s ease}
.dandelion-seed:hover{filter:brightness(1.4) drop-shadow(0 0 6px currentColor)}
.dandelion-seed-glow{pointer-events:none;opacity:0.4}
.dandelion-seed-pop{animation:seedPop 0.3s cubic-bezier(0.34,1.56,0.64,1) forwards}
@keyframes seedPop{from{transform:scale(0);opacity:0}to{transform:scale(1);opacity:1}}
.dandelion-seed.dimmed{opacity:0.2}
.dandelion-seed.focused{filter:brightness(1.2)}

/* ── Theme labels ── */
.dandelion-theme-label{font-size:12px;font-weight:600;font-family:'Segoe UI','Apple SD Gothic Neo',sans-serif;opacity:0;animation:fadeIn 0.3s ease-out 0.8s forwards}

/* ── Tooltip (seed click summary) — identical to app ── */
.dandelion-tooltip{
  position:fixed;display:none;
  max-width:280px;padding:14px 16px;
  background:rgba(22,27,34,0.95);border:1px solid rgba(255,255,255,0.08);
  border-radius:10px;box-shadow:0 8px 32px rgba(0,0,0,0.4);
  backdrop-filter:blur(12px);z-index:100;animation:fadeIn 0.2s ease-out}
.dandelion-tooltip.show{display:block}
.dandelion-tooltip-title{font-size:0.9em;font-weight:600;margin-bottom:6px}
.dandelion-tooltip-summary{font-size:0.78em;color:#8b949e;line-height:1.5;margin-bottom:8px}
.dandelion-tooltip-more{font-size:0.7em;color:#CE93D8;cursor:pointer;border:none;background:none;font-family:inherit;padding:0}
.dandelion-tooltip-more:hover{text-decoration:underline}
.dandelion-tooltip-weight{font-size:0.65em;color:#484f58;margin-top:6px}

/* ── Side panel (detail view) — identical to app ── */
.dandelion-panel{
  position:absolute;top:0;right:-400px;
  width:400px;height:100%;
  background:rgba(13,17,23,0.98);border-left:1px solid rgba(255,255,255,0.06);
  padding:24px;overflow-y:auto;transition:right 0.3s ease;z-index:200}
.dandelion-panel.open{right:0}
.dandelion-panel-close{position:absolute;top:12px;right:12px;background:none;border:none;color:#8b949e;font-size:1.2em;cursor:pointer}
.dandelion-panel-close:hover{color:#E6EDF3}
.dandelion-panel-title{font-size:1.1em;font-weight:600;margin-bottom:16px;padding-right:30px}
.dandelion-panel-detail{font-size:0.85em;line-height:1.7;color:#c9d1d9;margin-bottom:20px;white-space:pre-wrap}
.dandelion-panel-meta{font-size:0.72em;color:#484f58;padding-top:12px;border-top:1px solid rgba(255,255,255,0.04)}

/* ── "현재" label ── */
.dandelion-origin-label{font-size:11px;fill:#8b949e;text-anchor:middle;opacity:0;animation:fadeIn 0.3s ease-out 0.2s forwards}
</style>
</head>
<body>

<div class="dandelion-app">
  <!-- Header -->
  <div class="dandelion-header">
    <div style="display:flex;align-items:center">
      <h1>미래아이디어</h1>
      <span class="meta">{{QUERY}} &middot; {{CREATED_AT}}</span>
    </div>
  </div>

  <!-- Full-screen SVG canvas -->
  <div class="dandelion-canvas">
    <svg id="dandelion-svg"></svg>

    <!-- Side panel (detail view) -->
    <div class="dandelion-panel" id="dandelion-panel">
      <button class="dandelion-panel-close" onclick="dlClosePanel()">&times;</button>
      <div class="dandelion-panel-title" id="dandelion-panel-title"></div>
      <div class="dandelion-panel-detail" id="dandelion-panel-detail"></div>
      <div class="dandelion-panel-meta" id="dandelion-panel-meta"></div>
    </div>
  </div>
</div>

<!-- Tooltip (fixed position, outside canvas) -->
<div class="dandelion-tooltip" id="dandelion-tooltip">
  <div class="dandelion-tooltip-title" id="dandelion-tooltip-title"></div>
  <div class="dandelion-tooltip-summary" id="dandelion-tooltip-summary"></div>
  <div class="dandelion-tooltip-weight" id="dandelion-tooltip-weight"></div>
  <button class="dandelion-tooltip-more" id="dandelion-tooltip-more">자세히 보기 &rarr;</button>
</div>

<script>
(function(){
"use strict";

var THEMES = {{THEMES_JSON}};
var SEEDS  = {{SEEDS_JSON}};

var BASE_R = 16, SCALE = 6, W_CAP = 5;
var STEM_PAD = {top:80, bottom:80};
var focusedSeed = null;
var seedDataMap = {};  // id → {seed, el, glow, theme, origR}

// ── Draw SVG tree ──
var svg = document.getElementById('dandelion-svg');
var w = svg.clientWidth || svg.parentElement.clientWidth || 900;
var h = svg.clientHeight || svg.parentElement.clientHeight || 600;

// Defs
var defs = svgEl('defs',{});
var blur = svgEl('filter',{id:'dandelion-blur',x:'-50%',y:'-50%',width:'200%',height:'200%'});
blur.appendChild(svgEl('feGaussianBlur',{'in':'SourceGraphic',stdDeviation:'3'}));
defs.appendChild(blur);
svg.appendChild(defs);

var originX = w/2, originY = h - STEM_PAD.bottom;

// Origin circle
svg.appendChild(svgEl('circle',{cx:originX,cy:originY,r:6,class:'dandelion-origin'}));
var originLabel = svgEl('text',{x:originX,y:originY+20,class:'dandelion-origin-label'});
originLabel.textContent = '현재';
svg.appendChild(originLabel);

// Stems + seeds
var stemSpacing = w/5;

THEMES.forEach(function(theme,i){
  var topX = stemSpacing*(i+1), topY = STEM_PAD.top;
  var cp1x = originX+(topX-originX)*0.3, cp1y = originY-(originY-topY)*0.5;
  var cp2x = topX-(topX-originX)*0.1, cp2y = topY+(originY-topY)*0.2;
  var d = 'M '+originX+' '+originY+' C '+cp1x+' '+cp1y+', '+cp2x+' '+cp2y+', '+topX+' '+topY;

  var path = svgEl('path',{d:d,class:'dandelion-stem dandelion-stem-animate',stroke:theme.color,'stroke-opacity':'0.5','data-theme-id':theme.id});
  svg.appendChild(path);
  var len = estimatePathLength(originX,originY,cp1x,cp1y,cp2x,cp2y,topX,topY);
  path.style.strokeDasharray = len;
  path.style.strokeDashoffset = len;
  path.style.setProperty('--stem-length', len);

  // Theme label
  var lbl = svgEl('text',{x:topX,y:topY-12,fill:theme.color,'text-anchor':'middle',class:'dandelion-theme-label'});
  lbl.textContent = theme.name;
  svg.appendChild(lbl);

  // Seeds for this theme
  var themeSeeds = SEEDS.filter(function(s){return s.theme_id===theme.id;});
  themeSeeds.sort(function(a,b){return a.time_months-b.time_months;});
  var cnt = themeSeeds.length;

  themeSeeds.forEach(function(seed,idx){
    var spreadT = 0.15 + (idx/Math.max(cnt-1,1))*0.7;
    var pt = cubicPoint(originX,originY,cp1x,cp1y,cp2x,cp2y,topX,topY,spreadT);
    var offset = (idx%2===0?1:-1)*(25+pseudoRandom(seed.id)*20);

    var r = BASE_R + (Math.min(seed.weight,W_CAP)-1)*SCALE;
    var cx = pt.x+offset, cy = pt.y;

    // Gradient
    var gradId = 'grad-'+seed.id;
    var grad = svgEl('radialGradient',{id:gradId,cx:'45%',cy:'45%',r:'55%'});
    grad.innerHTML = '<stop offset="0%" stop-color="'+theme.color+'" stop-opacity="0.95"/>'
      +'<stop offset="50%" stop-color="'+theme.color+'" stop-opacity="0.5"/>'
      +'<stop offset="100%" stop-color="'+theme.color+'" stop-opacity="0.08"/>';
    defs.appendChild(grad);

    // Glow
    var glow = svgEl('circle',{cx:cx,cy:cy,r:r*1.6,fill:'url(#'+gradId+')',filter:'url(#dandelion-blur)',
      class:'dandelion-seed-glow dandelion-seed-pop',
      style:'animation-delay:'+(idx*0.15)+'s;transform:scale(0);opacity:0;pointer-events:none'});
    svg.appendChild(glow);

    // Core seed circle
    var circle = svgEl('circle',{cx:cx,cy:cy,r:r,fill:'url(#'+gradId+')',
      class:'dandelion-seed dandelion-seed-pop','data-seed-id':seed.id,
      style:'animation-delay:'+(idx*0.15)+'s;transform:scale(0);opacity:0'});

    circle.addEventListener('click',function(e){
      e.stopPropagation();
      onSeedClick(seed,circle,theme);
    });

    svg.appendChild(circle);

    seedDataMap[seed.id] = {seed:seed, el:circle, glow:glow, theme:theme, origR:r};
  });
});

// ── Seed click (hybrid C→B — identical to app) ──
function onSeedClick(seed, el, theme){
  // Reset previous focus
  if(focusedSeed){
    document.querySelectorAll('.dandelion-seed').forEach(function(s){s.classList.remove('dimmed','focused');});
    // Restore previous seed radius
    var prev = seedDataMap[focusedSeed.id];
    if(prev) prev.el.setAttribute('r', prev.origR);
  }

  // Toggle off if clicking same seed
  if(focusedSeed && focusedSeed.id === seed.id){
    focusedSeed = null;
    hideTooltip();
    return;
  }

  focusedSeed = seed;

  // Dim others, focus this one
  document.querySelectorAll('.dandelion-seed').forEach(function(s){
    if(s.getAttribute('data-seed-id') !== seed.id) s.classList.add('dimmed');
  });
  el.classList.add('focused');

  // Enlarge 1.5x
  var data = seedDataMap[seed.id];
  el.setAttribute('r', data.origR * 1.5);

  // Show tooltip
  showTooltip(seed, el, theme);
}

// ── Tooltip ──
var tooltip = document.getElementById('dandelion-tooltip');

function showTooltip(seed, el, theme){
  document.getElementById('dandelion-tooltip-title').textContent = seed.title;
  document.getElementById('dandelion-tooltip-title').style.color = theme.color;
  document.getElementById('dandelion-tooltip-summary').textContent = seed.summary;

  var weightEl = document.getElementById('dandelion-tooltip-weight');
  if(seed.source_count > 1){
    weightEl.textContent = 'AI '+seed.source_count+'명이 유사하게 상상';
    weightEl.style.display = 'block';
  } else {
    weightEl.style.display = 'none';
  }

  var svgRect = svg.getBoundingClientRect();
  var cx = parseFloat(el.getAttribute('cx'));
  var cy = parseFloat(el.getAttribute('cy'));

  // Scale SVG coords to screen coords
  var scaleX = svgRect.width / w;
  var scaleY = svgRect.height / h;
  var left = svgRect.left + cx*scaleX + 20;
  var top = svgRect.top + cy*scaleY - 40;

  tooltip.style.left = '0px';
  tooltip.style.top = '0px';
  tooltip.classList.add('show');

  var tw = tooltip.offsetWidth;
  var th = tooltip.offsetHeight;
  var vw = window.innerWidth;
  var vh = window.innerHeight;

  if(left + tw > vw - 10) left = svgRect.left + cx*scaleX - tw - 20;
  if(top + th > vh - 10) top = vh - th - 10;
  if(top < 10) top = 10;
  if(left < 10) left = 10;

  tooltip.style.left = left+'px';
  tooltip.style.top = top+'px';

  document.getElementById('dandelion-tooltip-more').onclick = function(){
    dlOpenPanel(seed, theme);
  };
}

function hideTooltip(){
  tooltip.classList.remove('show');
  // Restore all seed radii
  document.querySelectorAll('.dandelion-seed').forEach(function(s){
    var sid = s.getAttribute('data-seed-id');
    var data = seedDataMap[sid];
    if(data) s.setAttribute('r', data.origR);
  });
}

// ── Side panel ──
function dlOpenPanel(seed, theme){
  var panel = document.getElementById('dandelion-panel');
  document.getElementById('dandelion-panel-title').textContent = seed.title;
  document.getElementById('dandelion-panel-title').style.color = theme.color;
  document.getElementById('dandelion-panel-detail').textContent = seed.detail;

  var meta = '시점: '+seed.time_months+'개월 후';
  if(seed.source_count > 1) meta += ' · AI '+seed.source_count+'명 동의';
  document.getElementById('dandelion-panel-meta').textContent = meta;

  panel.classList.add('open');
}
window.dlClosePanel = function(){
  document.getElementById('dandelion-panel').classList.remove('open');
};

// ── Click outside to unfocus ──
document.addEventListener('click',function(e){
  if(!e.target.closest('.dandelion-seed') && !e.target.closest('.dandelion-tooltip') && !e.target.closest('.dandelion-panel')){
    focusedSeed = null;
    hideTooltip();
    document.querySelectorAll('.dandelion-seed').forEach(function(s){s.classList.remove('dimmed','focused');});
    dlClosePanel();
  }
});

// ── Helpers ──
function svgEl(tag,attrs){
  var el = document.createElementNS('http://www.w3.org/2000/svg',tag);
  for(var k in attrs) el.setAttribute(k,attrs[k]);
  return el;
}

function pseudoRandom(id){
  var h=0;for(var i=0;i<id.length;i++){h=((h<<5)-h)+id.charCodeAt(i);h|=0;}
  return Math.abs(h%100)/100;
}

function cubicPoint(x0,y0,x1,y1,x2,y2,x3,y3,t){
  var u=1-t;
  return{x:u*u*u*x0+3*u*u*t*x1+3*u*t*t*x2+t*t*t*x3, y:u*u*u*y0+3*u*u*t*y1+3*u*t*t*y2+t*t*t*y3};
}

function estimatePathLength(x0,y0,x1,y1,x2,y2,x3,y3){
  var len=0,prev={x:x0,y:y0};
  for(var i=1;i<=20;i++){
    var p=cubicPoint(x0,y0,x1,y1,x2,y2,x3,y3,i/20);
    var dx=p.x-prev.x,dy=p.y-prev.y;
    len+=Math.sqrt(dx*dx+dy*dy);prev=p;
  }
  return len;
}

})();
</script>
</body>
</html>
"""
