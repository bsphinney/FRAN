/* DE-LIMP Corpus Browser — SPA */
const $ = (s, r=document) => r.querySelector(s);
const view = $('#view');
let charts = {};
// UC Davis-led palette: Aggie Gold + Aggie Blue + UCD secondary colors
const PALETTE = ['#FFBF00','#00B5E2','#6FCFEB','#FFCF40','#3f87c5','#C99700','#6CCA98','#F18A00','#022851','#B0D2E8','#E0A800','#8AB8D9'];

/* ---------- helpers ---------- */
const fmt = n => n==null ? '—' : Number(n).toLocaleString('en-US');
const fmtF = (n,d=2) => n==null ? '—' : Number(n).toFixed(d);
const sci = n => n==null ? '—' : (Math.abs(n)>=1e4 ? Number(n).toExponential(2) : Number(n).toLocaleString('en-US',{maximumFractionDigits:1}));
// Core-facility internal key: ?key=... (persisted) -> sent as X-Internal-Key to reveal real filenames.
const INTKEY = (new URLSearchParams(location.search).get('key')) || localStorage.getItem('delimp_ikey') || '';
if(INTKEY && new URLSearchParams(location.search).get('key')) localStorage.setItem('delimp_ikey', INTKEY);
async function api(path){ const r = await fetch(path, INTKEY?{headers:{'X-Internal-Key':INTKEY}}:{}); if(!r.ok){ const e=await r.json().catch(()=>({detail:r.statusText})); throw new Error(e.detail||('HTTP '+r.status)); } return r.json(); }
function esc(s){ return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
// clickable gene cell inside a row that already has an onclick (stops row navigation)
function geneCell(g){ return g?`<span onclick="event.stopPropagation();go('gene','${encodeURIComponent(g)}')" class="cursor-pointer text-accent-400 hover:underline">${esc(g)}</span>`:'—'; }
function copy(t){ navigator.clipboard.writeText(t); toast('Copied'); }
// Shareable permalink: the hash router makes location.href a stable, openable link
// for the current peptide/protein (like a GPMDB accession URL). Copy it to clipboard.
function shareLink(){ navigator.clipboard.writeText(location.href); toast('Link copied — paste it anywhere to share this page'); }
let toastT;
function toast(msg){ let el=$('#toast'); if(!el){ el=document.createElement('div'); el.id='toast'; el.className='fixed bottom-5 left-1/2 -translate-x-1/2 bg-ink-700 text-white text-sm px-4 py-2 rounded-xl shadow-glow z-50 transition-opacity'; document.body.appendChild(el);} el.textContent=msg; el.style.opacity=1; clearTimeout(toastT); toastT=setTimeout(()=>el.style.opacity=0,1400); }
function destroyCharts(){ Object.values(charts).forEach(c=>{try{c.destroy()}catch(e){}}); charts={}; }
function chartColors(){ const dark=document.documentElement.classList.contains('dark'); return { grid: dark?'rgba(255,255,255,.06)':'rgba(15,21,37,.08)', tick: dark?'#94a3b8':'#475569' }; }

/* ---------- theme ---------- */
function toggleTheme(){ document.documentElement.classList.toggle('dark'); localStorage.delimpTheme=document.documentElement.classList.contains('dark')?'dark':'light'; route(); }
if(localStorage.delimpTheme==='light') document.documentElement.classList.remove('dark');

/* ---------- router ---------- */
function go(view, param){ location.hash = param!==undefined ? `#/${view}/${encodeURIComponent(param)}` : `#/${view}`; }
window.addEventListener('hashchange', route);

function setActiveNav(v){ document.querySelectorAll('.navbtn').forEach(b=>{ b.classList.toggle('tab-active', b.dataset.view===v); }); }

function route(){
  const h = location.hash.replace(/^#\/?/,'');
  const [v, ...rest] = h.split('/');
  const param = rest.map(decodeURIComponent).join('/');
  destroyCharts();
  setActiveNav(v||'dashboard');
  switch(v){
    case '': case 'dashboard': return renderDashboard();
    case 'searches': return renderSearches();
    case 'ionmobility': return renderIonMobility();
    case 'highlights': return renderHighlights();
    case 'peptide': return renderPeptide(param);
    case 'protein': return renderProtein(param);
    case 'gene': return renderGene(param);
    case 'searchresults': return renderSearchResults(param);
    case 'run': return renderSearchDetail(param);
    default: return renderDashboard();
  }
}

/* ---------- live status + counts ticker ---------- */
async function pollHealth(){
  try{ const h = await api('/health'); const dot=$('#liveDot').firstElementChild;
    if(h.version){ const v='v'+h.version; const vb=$('#appVersion'); if(vb) vb.textContent=v; }
    if(h.connected){ dot.className='w-2 h-2 rounded-full bg-emerald-400 animate-pulse'; $('#liveStatus').textContent='live · read-only'; }
    else { dot.className='w-2 h-2 rounded-full bg-amber-400'; $('#liveStatus').textContent='db offline'; }
  }catch(e){ const dot=$('#liveDot').firstElementChild; dot.className='w-2 h-2 rounded-full bg-rose-500'; $('#liveStatus').textContent='no db'; }
}
async function refreshFooterCounts(){
  try{ const c = await api('/api/counts');
    $('#footCounts').textContent = `${fmt(c.precursors)} precursors · ${fmt(c.distinct_peptides)} peptides · ${fmt(c.distinct_protein_groups)} protein groups`;
    $('#lastUpdated').textContent = new Date().toLocaleTimeString();
  }catch(e){}
}

/* animated counter */
function animateCount(el, to){ if(to==null){ el.textContent='—'; return; } const from = Number(el.dataset.v||0); const dur=700, t0=performance.now();
  function step(t){ const k=Math.min(1,(t-t0)/dur); const e=1-Math.pow(1-k,3); el.textContent=fmt(Math.round(from+(to-from)*e)); if(k<1)requestAnimationFrame(step); else el.dataset.v=to; }
  requestAnimationFrame(step); }

/* ---------- DASHBOARD ---------- */
function kpi(label, id, sub, accent){ return `
  <div class="glass card p-5 fade-in relative overflow-hidden">
    <div class="absolute -right-6 -top-6 w-24 h-24 rounded-full blur-2xl opacity-30" style="background:${accent}"></div>
    <div class="text-[11px] uppercase tracking-wider text-slate-400">${label}</div>
    <div id="${id}" data-v="0" class="kpi-num text-3xl font-extrabold text-white mt-1">—</div>
    <div class="text-[11px] text-slate-500 mt-1">${sub}</div>
  </div>`; }

async function renderDashboard(){
  view.innerHTML = `
  <section class="mb-6 fade-in">
    <div class="flex items-end justify-between flex-wrap gap-3">
      <div>
        <h1 class="text-2xl font-extrabold text-white tracking-tight">Corpus Overview</h1>
        <p class="text-slate-400 text-sm mt-1">Live snapshot of <span class="text-accent-400 font-semibold">FRAN</span> — the DE-LIMP proteomics corpus (Fragment Reference &amp; ANnotation). Watch it grow as searches ingest.</p>
      </div>
      <button onclick="renderDashboard()" class="text-sm px-3 py-2 rounded-xl glass hover:text-white text-slate-300 flex items-center gap-2">
        <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>Refresh
      </button>
    </div>
  </section>
  <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
    ${kpi('Precursors','k_prec','peptide-spectrum observations','#5b8cff')}
    ${kpi('Distinct peptides','k_pep','unique stripped sequences','#a78bfa')}
    ${kpi('Protein groups','k_prot','distinct groups','#2dd4bf')}
    ${kpi('Searches','k_search','ingested search runs','#f472b6')}
  </div>
  <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
    ${kpi('Raw files','k_raw','runs in corpus','#fbbf24')}
    ${kpi('Organisms','k_org','distinct taxa','#34d399')}
    ${kpi('Ion-mobility precursors','k_im','with 1/K0 (our differentiator)','#22d3ee')}
    ${kpi('Protein rows','k_protrows','per-search per-run','#c084fc')}
  </div>

  <div class="grid lg:grid-cols-3 gap-4 mb-6">
    <div class="glass card p-5 fade-in lg:col-span-2">
      <div class="flex items-center justify-between mb-3"><h3 class="font-bold text-white">Ion mobility × retention time</h3>
      <div class="flex items-center gap-3">
        <button id="c_im_toggle" onclick="toggleIMMode('c_im')" class="text-xs px-2 py-1 rounded-lg border border-white/10 text-slate-300 hover:text-white hover:border-accent/40">iRT × peptide count</button>
        <a onclick="go('ionmobility')" class="text-xs text-accent-400 cursor-pointer hover:underline">Open full view →</a></div></div>
      <div class="text-[11px] text-slate-500 mb-2">Retention time vs 1/K₀ ion mobility — one point per distinct precursor. The IM dimension GPMDB can't show. <span class="text-slate-600">(Axis is raw RT: the current runs were ingested before iRT capture; the axis switches to cross-run iRT automatically for searches ingested with indexed RT.)</span></div>
      <div class="h-72"><canvas id="c_im"></canvas></div>
    </div>
    <div class="glass card p-5 fade-in">
      <h3 class="font-bold text-white mb-3">Charge states</h3>
      <div class="h-72"><canvas id="c_charge"></canvas></div>
    </div>
  </div>

  <div class="glass card p-5 fade-in mb-6">
    <div class="flex items-center justify-between mb-1"><h3 class="font-bold text-white">🪰 Flyability × observed intensity</h3>
    <span class="text-[10px] text-slate-500">Koina PFly · one point per peptide · click a point</span></div>
    <p class="text-[11px] text-slate-500 mb-3">Predicted <b>flyability</b> (how readily a peptide ionizes &amp; is detected — sequence-intrinsic) vs its <b>mean observed precursor intensity</b> across the corpus. Strong flyers should trend higher. Colored by flyability tier.</p>
    <div class="h-80"><canvas id="c_fly"></canvas></div>
  </div>

  <div class="grid lg:grid-cols-3 gap-4 mb-6">
    <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-3">Species</h3><div class="h-64"><canvas id="c_species"></canvas></div></div>
    <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-3">Platform / acquisition</h3><div class="h-64"><canvas id="c_platform"></canvas></div></div>
    <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-3">Search engines</h3><div class="h-64"><canvas id="c_engine"></canvas></div></div>
  </div>

  <div class="glass card p-5 fade-in">
    <div class="flex items-center justify-between mb-3"><h3 class="font-bold text-white">Recently ingested searches</h3><a onclick="go('searches')" class="text-xs text-accent-400 cursor-pointer hover:underline">All searches →</a></div>
    <div id="recentSearches" class="space-y-2"><div class="skeleton h-12 rounded-xl"></div><div class="skeleton h-12 rounded-xl"></div></div>
  </div>`;

  try{
    const d = await api('/api/overview');
    const c = d.counts;
    animateCount($('#k_prec'), c.precursors); animateCount($('#k_pep'), c.distinct_peptides);
    animateCount($('#k_prot'), c.distinct_protein_groups); animateCount($('#k_search'), c.searches);
    animateCount($('#k_raw'), c.raw_files); animateCount($('#k_org'), c.organisms);
    animateCount($('#k_im'), c.im_bearing_precursors); animateCount($('#k_protrows'), c.proteins);

    drawCharge(d.charges); drawSpecies(d.species); drawPlatform(d.platforms); drawEngine(d.engines);
    drawRecent(d.recent_searches);
    loadIMScatter('c_im', null, 4000);
    loadFlyabilityScatter();
  }catch(e){ dbError(e); }
}

function drawRecent(rows){
  const el = $('#recentSearches');
  if(!rows||!rows.length){ el.innerHTML = empty('No searches ingested yet.'); return; }
  el.innerHTML = rows.map(s=>`
    <div onclick="go('run','${s.id}')" class="row-hover cursor-pointer rounded-xl border border-white/5 px-4 py-3 flex items-center gap-3">
      <span class="px-2 py-1 rounded-md text-[10px] font-bold ${engBadge(s.search_engine)}">${esc((s.search_engine||'?').toUpperCase())}</span>
      <div class="min-w-0 flex-1">
        <div class="font-medium text-white truncate">${esc(s.search_name||'(unnamed)')}</div>
        <div class="text-[11px] text-slate-500">${esc(s.pipeline_id||'')} · v${esc(s.delimp_version||'?')} · ${s.ingested_at?new Date(s.ingested_at).toLocaleString():''}</div>
      </div>
      <div class="text-right hidden sm:block"><div class="text-sm font-semibold text-white kpi-num">${fmt(s.n_precursors_total)}</div><div class="text-[10px] text-slate-500">precursors</div></div>
      <div class="text-right"><div class="text-sm font-semibold text-white kpi-num">${fmt(s.n_raw_files)}</div><div class="text-[10px] text-slate-500">runs</div></div>
      <span class="px-2 py-1 rounded-md text-[10px] ${statusBadge(s.status)}">${esc(s.status||'?')}</span>
    </div>`).join('');
}
function engBadge(e){ return {diann:'bg-accent/20 text-accent-400',sage:'bg-plum/20 text-plum',spectronaut:'bg-teal/20 text-teal',fragpipe:'bg-amber-500/20 text-amber-300'}[e]||'bg-slate-500/20 text-slate-300'; }
function statusBadge(s){ return {completed:'bg-emerald-500/15 text-emerald-300',running:'bg-accent/15 text-accent-400',failed:'bg-rose-500/15 text-rose-300',queued:'bg-amber-500/15 text-amber-300'}[s]||'bg-slate-500/15 text-slate-300'; }

/* ---------- charts ---------- */
function drawCharge(rows){ const cc=chartColors(); const ctx=$('#c_charge'); if(!ctx)return;
  charts.charge=new Chart(ctx,{type:'bar',data:{labels:rows.map(r=>r.charge+'+'),datasets:[{data:rows.map(r=>r.n),backgroundColor:rows.map((_,i)=>PALETTE[i%PALETTE.length]),borderRadius:8}]},
  options:{plugins:{legend:{display:false}},scales:{x:{grid:{display:false},ticks:{color:cc.tick}},y:{grid:{color:cc.grid},ticks:{color:cc.tick,callback:v=>sci(v)}}},maintainAspectRatio:false}}); }
function drawSpecies(rows){ const cc=chartColors(); const ctx=$('#c_species'); if(!ctx)return;
  charts.species=new Chart(ctx,{type:'doughnut',data:{labels:rows.map(r=>r.organism),datasets:[{data:rows.map(r=>r.n_runs),backgroundColor:PALETTE,borderWidth:0}]},
  options:{cutout:'62%',plugins:{legend:{position:'right',labels:{color:cc.tick,boxWidth:10,font:{size:10}}}},maintainAspectRatio:false}}); }
function drawPlatform(rows){ const cc=chartColors(); const ctx=$('#c_platform'); if(!ctx)return;
  const agg={}; rows.forEach(r=>{const k=`${r.platform} / ${r.acquisition_method}`; agg[k]=(agg[k]||0)+Number(r.n_runs);});
  const labels=Object.keys(agg);
  charts.platform=new Chart(ctx,{type:'bar',data:{labels,datasets:[{data:labels.map(k=>agg[k]),backgroundColor:labels.map((_,i)=>PALETTE[i%PALETTE.length]),borderRadius:8}]},
  options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:{grid:{color:cc.grid},ticks:{color:cc.tick}},y:{grid:{display:false},ticks:{color:cc.tick,font:{size:10}}}},maintainAspectRatio:false}}); }
function drawEngine(rows){ const cc=chartColors(); const ctx=$('#c_engine'); if(!ctx)return;
  charts.engine=new Chart(ctx,{type:'polarArea',data:{labels:rows.map(r=>r.search_engine),datasets:[{data:rows.map(r=>r.n_searches),backgroundColor:PALETTE.map(c=>c+'cc'),borderWidth:0}]},
  options:{plugins:{legend:{position:'bottom',labels:{color:cc.tick,boxWidth:10,font:{size:10}}}},scales:{r:{grid:{color:cc.grid},ticks:{display:false}}},maintainAspectRatio:false}}); }

let imData={}, imMode={};   // per-canvas cache of the fetched sample + current view mode

async function loadIMScatter(canvasId, searchId, n){ const ctx=$('#'+canvasId); if(!ctx)return;
  try{
    const d = await api(`/api/im_density?n=${n}${searchId?`&search_id=${encodeURIComponent(searchId)}`:''}`);
    if(!d.points || !d.points.length){ ctx.parentElement.innerHTML = empty('Retention-time axis not yet populated for this corpus — the RT × 1/K₀ map appears once retention times are ingested.'); return; }
    imData[canvasId]=d;
    renderIMChart(canvasId, imMode[canvasId]||'scatter');
  }catch(e){ ctx.parentElement.innerHTML = empty('No ion-mobility data available yet.'); }
}

function toggleIMMode(canvasId){
  imMode[canvasId] = (imMode[canvasId]==='hist') ? 'scatter' : 'hist';
  const btn=$('#'+canvasId+'_toggle');
  if(btn) btn.textContent = imMode[canvasId]==='hist' ? '1/K₀ × iRT map' : 'iRT × peptide count';
  renderIMChart(canvasId, imMode[canvasId]);
}

function renderIMChart(canvasId, mode){
  const cc=chartColors(); const ctx=$('#'+canvasId); const d=imData[canvasId]; if(!ctx||!d)return;
  if(charts[canvasId]) charts[canvasId].destroy();
  const xlabel = d.x_axis || 'Retention time (min)';
  if(mode==='hist'){
    // iRT (or RT) × peptide number: bin the sampled distinct precursors, count per bin.
    const xs = d.points.map(p=>p.rt).filter(v=>v!=null && isFinite(v));
    if(!xs.length){ return; }
    const lo=Math.min(...xs), hi=Math.max(...xs); const NB=40; const w=((hi-lo)/NB)||1;
    const bins=new Array(NB).fill(0);
    xs.forEach(v=>{ let b=Math.floor((v-lo)/w); if(b<0)b=0; if(b>=NB)b=NB-1; bins[b]++; });
    const labels=bins.map((_,i)=>fmtF(lo+(i+0.5)*w,0));
    charts[canvasId]=new Chart(ctx,{type:'bar',data:{labels,datasets:[{label:'peptides',data:bins,backgroundColor:'#22d3ee99',borderRadius:3}]},
      options:{plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>`${c.parsed.y} peptides · ${xlabel} ≈ ${c.label}`}}},
        scales:{x:{title:{display:true,text:xlabel,color:cc.tick},grid:{display:false},ticks:{color:cc.tick,maxTicksLimit:12}},
                y:{title:{display:true,text:'distinct peptides (sample)',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}}},maintainAspectRatio:false}});
    return;
  }
  const byCharge={}; d.points.forEach(p=>{ const z=p.charge||0; (byCharge[z] ||= []).push({x:p.rt,y:p.im}); });
  const ds=Object.keys(byCharge).sort().map((z,i)=>({label:z+'+',data:byCharge[z],backgroundColor:PALETTE[i%PALETTE.length]+'88',pointRadius:2,pointHoverRadius:4}));
  charts[canvasId]=new Chart(ctx,{type:'scatter',data:{datasets:ds},options:{plugins:{legend:{position:'bottom',labels:{color:cc.tick,boxWidth:8,font:{size:10}}},tooltip:{callbacks:{label:c=>`${xlabel} ${fmtF(c.parsed.x)} · 1/K₀ ${fmtF(c.parsed.y,3)}`}}},scales:{x:{title:{display:true,text:xlabel,color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}},y:{title:{display:true,text:'1/K₀ (Vs/cm²)',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}}},maintainAspectRatio:false}});
}

/* ---------- ION MOBILITY full view ---------- */
async function renderIonMobility(){
  view.innerHTML=`<section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">Ion Mobility Showcase</h1>
  <p class="text-slate-400 text-sm mt-1">Per-precursor 1/K₀ ion mobility vs retention time — one point per distinct precursor, the dimension GPMDB never had. Colored by charge. <span class="text-slate-500">(Axis is raw RT until searches are ingested with indexed RT, then it switches to cross-run-comparable iRT.)</span></p></section>
  <div class="glass card p-5 fade-in"><div class="flex items-center gap-3 mb-3"><h3 class="font-bold text-white">RT × 1/K₀ density</h3>
  <button id="c_imbig_toggle" onclick="toggleIMMode('c_imbig')" class="ml-auto text-xs px-2 py-1 rounded-lg border border-white/10 text-slate-300 hover:text-white hover:border-accent/40">iRT × peptide count</button>
  <select id="imN" onchange="loadIMScatter('c_imbig',null,this.value)" class="bg-ink-800 border border-white/10 rounded-lg px-2 py-1 text-sm">
  <option value="4000">4k points</option><option value="8000" selected>8k points</option><option value="15000">15k points</option></select></div>
  <div class="h-[560px]"><canvas id="c_imbig"></canvas></div></div>`;
  loadIMScatter('c_imbig', null, 8000);
}

/* ---------- SEARCHES list ---------- */
async function renderSearches(){
  view.innerHTML=`<section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">Searches</h1>
  <p class="text-slate-400 text-sm mt-1">Every ingested DE-LIMP search run and its per-run statistics.</p></section>
  <div class="glass card overflow-hidden fade-in"><div id="searchesTable" class="p-4"><div class="skeleton h-40 rounded-xl"></div></div></div>`;
  try{ const d = await api('/api/searches?limit=100');
    if(!d.rows.length){ $('#searchesTable').innerHTML=empty('No searches ingested yet — they will appear here as ingest runs.'); return; }
    $('#searchesTable').innerHTML = table(
      ['Search','Engine','Pipeline','Runs','Precursors','Proteins','Status','Ingested'],
      d.rows.map(s=>[
        `<span class="font-medium text-white">${esc(s.search_name||'(unnamed)')}</span>`,
        `<span class="px-2 py-0.5 rounded text-[10px] font-bold ${engBadge(s.search_engine)}">${esc((s.search_engine||'?').toUpperCase())}</span>`,
        `<span class="text-slate-400 text-xs">${esc(s.pipeline_id||'')}</span>`,
        fmt(s.n_raw_files), fmt(s.n_precursors_total), fmt(s.n_proteins_total),
        `<span class="px-2 py-0.5 rounded text-[10px] ${statusBadge(s.status)}">${esc(s.status||'?')}</span>`,
        s.ingested_at?new Date(s.ingested_at).toLocaleDateString():'—'
      ]), d.rows.map(s=>`go('run','${s.id}')`));
  }catch(e){ dbError(e,'#searchesTable'); }
}

async function renderSearchDetail(id){
  view.innerHTML=`<div class="skeleton h-64 rounded-xl"></div>`;
  try{ const d=await api(`/api/search/${encodeURIComponent(id)}`); const s=d.summary;
    view.innerHTML=`
    ${crumb([['Searches','searches'],[s.search_name||'search',null]])}
    <div class="glass card p-6 fade-in mb-5">
      <div class="flex items-start justify-between flex-wrap gap-3">
        <div><h1 class="text-2xl font-extrabold text-white">${esc(s.search_name||'(unnamed)')}</h1>
        <div class="text-sm text-slate-400 mt-1">${esc(s.pipeline_id||'')} ${s.pipeline_version?'· '+esc(s.pipeline_version):''} · DE-LIMP v${esc(s.delimp_version||'?')}</div></div>
        <div class="flex gap-2"><span class="px-3 py-1 rounded-lg text-xs font-bold ${engBadge(s.search_engine)}">${esc((s.search_engine||'?').toUpperCase())} ${esc(s.search_engine_version||'')}</span>
        <span class="px-3 py-1 rounded-lg text-xs ${statusBadge(s.status)}">${esc(s.status||'?')}</span></div>
      </div>
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-4 mt-5">
        ${stat('Raw files',fmt(s.n_raw_files))}${stat('Precursors',fmt(s.n_precursors_total))}
        ${stat('Proteins',fmt(s.n_proteins_total))}${stat('FASTA proteins',fmt(s.fasta_n_proteins))}
      </div>
      ${s.fasta_path?`<div class="mt-4 text-xs text-slate-500">FASTA: <code class="text-slate-400">${esc(s.fasta_path)}</code></div>`:''}
      ${s.doi?`<div class="mt-1 text-xs text-slate-500">DOI: ${esc(s.doi)}</div>`:''}
    </div>
    <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-3">Runs (${d.runs.length})</h3>
    ${d.runs.length?table(['Run','Platform','Acquisition','Instrument','Organism','Precursors','Proteins'],
      d.runs.map(r=>[`<span class="font-mono text-xs text-white">${esc(r.raw_basename||r.raw_path)}</span>`,esc(r.platform||'—'),esc(r.acquisition_method||'—'),esc(r.instrument_model||'—'),esc(r.organism_name||'—'),fmt(r.n_precursors),fmt(r.n_proteins)])):empty('No run rows.')}</div>`;
  }catch(e){ dbError(e); }
}

/* ---------- SEARCH RESULTS (peptide + protein) ---------- */
function doGlobalSearch(id){ const el=$('#'+(id||'globalSearch')); const q=((el&&el.value)||'').trim(); if(q.length<2)return toast('Type at least 2 characters'); closeMobileMenu(); go('searchresults', q); }
function toggleMobileMenu(){ const m=$('#mobileMenu'); if(m) m.classList.toggle('hidden'); }
function closeMobileMenu(){ const m=$('#mobileMenu'); if(m) m.classList.add('hidden'); }
async function renderSearchResults(q){
  view.innerHTML=`<section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">Search results</h1>
  <p class="text-slate-400 text-sm mt-1">for “<span class="text-accent-400 font-mono">${esc(q)}</span>”</p>
  <div class="flex gap-2 mt-3 text-sm"><button id="tab_pep" onclick="srTab('pep')" class="px-3 py-1.5 rounded-lg tab-active">Peptides</button>
  <button id="tab_prot" onclick="srTab('prot')" class="px-3 py-1.5 rounded-lg glass text-slate-300">Proteins / genes</button>
  <label class="ml-auto flex items-center gap-2 text-xs text-slate-400"><input type="checkbox" id="exactChk" onchange="srTab(curSrTab)"> exact peptide match</label></div></section>
  <div class="glass card p-4 fade-in" id="srBody"><div class="skeleton h-40 rounded-xl"></div></div>`;
  window._srq=q;
  // Probe both result types so neither is hidden: badge the tabs with counts,
  // and auto-open the populated one (so a gene/protein query like "ALB" isn't
  // stranded on an empty Peptides tab).
  try{
    const [pep,prot]=await Promise.all([
      api(`/api/search/peptides?q=${encodeURIComponent(q)}&limit=1`).catch(()=>({total:0})),
      api(`/api/search/proteins?q=${encodeURIComponent(q)}&limit=1`).catch(()=>({total:0}))
    ]);
    const pt=pep.total||0, rt=prot.total||0, tp=$('#tab_pep'), tr=$('#tab_prot');
    if(tp) tp.innerHTML=`Peptides${pt?` <span class="opacity-60">(${fmt(pt)})</span>`:''}`;
    if(tr) tr.innerHTML=`Proteins / genes${rt?` <span class="opacity-60">(${fmt(rt)})</span>`:''}`;
    srTab(pt===0 && rt>0 ? 'prot' : 'pep');
  }catch(e){ srTab('pep'); }
}
let curSrTab='pep';
async function srTab(which){
  curSrTab=which; const q=window._srq;
  $('#tab_pep').className='px-3 py-1.5 rounded-lg '+(which==='pep'?'tab-active':'glass text-slate-300');
  $('#tab_prot').className='px-3 py-1.5 rounded-lg '+(which==='prot'?'tab-active':'glass text-slate-300');
  const body=$('#srBody'); body.innerHTML=`<div class="skeleton h-40 rounded-xl"></div>`;
  try{
    if(which==='pep'){
      const exact=$('#exactChk')?.checked?'&exact=true':'';
      const d=await api(`/api/search/peptides?q=${encodeURIComponent(q)}${exact}&limit=100`);
      if(!d.rows.length){ body.innerHTML=empty('No peptides match.'); return; }
      body.innerHTML = `<div class="text-xs text-slate-500 mb-3">${fmt(d.total)} matching peptides</div>`+table(
        ['Peptide','Precursors','Mod-forms','Charges','Runs','Searches','Best q','IM','Engines'],
        d.rows.map(r=>[`<span class="font-mono text-white">${esc(r.stripped_seq)}</span>`,fmt(r.n_precursors),fmt(r.n_modforms),fmt(r.n_charges),fmt(r.n_runs),fmt(r.n_searches),sci(r.best_q_value),r.has_im?'<span class="text-teal">●</span>':'<span class="text-slate-600">—</span>',r.max_engines>1?`<span class="text-plum font-semibold">${r.max_engines}×</span>`:'1']),
        d.rows.map(r=>`go('peptide','${encodeURIComponent(r.stripped_seq)}')`));
    } else {
      const d=await api(`/api/search/proteins?q=${encodeURIComponent(q)}&limit=100`);
      if(!d.rows.length){ body.innerHTML=empty('No proteins or genes match.'); return; }
      body.innerHTML = `<div class="text-xs text-slate-500 mb-3">${fmt(d.total)} matching protein groups</div>`+table(
        ['Protein group','Gene','Searches','Runs','Unique peptides','Precursors','Contaminant'],
        d.rows.map(r=>[`<span class="font-mono text-white">${esc(r.protein_group)}</span>`,geneCell(r.gene),fmt(r.n_searches),fmt(r.n_runs),fmt(r.sum_unique_peptides),fmt(r.sum_precursors),r.any_contaminant?'<span class="text-rose-400">yes</span>':'—']),
        d.rows.map(r=>`go('protein','${encodeURIComponent(r.protein_group)}')`));
    }
  }catch(e){ dbError(e,'#srBody'); }
}

/* ---------- PEPTIDE detail ---------- */
async function renderPeptide(seq){
  view.innerHTML=`<div class="skeleton h-64 rounded-xl"></div>`;
  try{ const d=await api(`/api/peptide/${encodeURIComponent(seq)}`); const s=d.summary;
    view.innerHTML=`
    ${crumb([['Search','searchresults',seq],['Peptide',null]])}
    <div class="glass card p-6 fade-in mb-5">
      <div class="flex items-center gap-3 flex-wrap">
        <h1 class="text-2xl font-extrabold text-white font-mono break-all">${esc(s.stripped_seq)}</h1>
        <button onclick="copy('${esc(s.stripped_seq)}')" class="text-xs px-2 py-1 rounded-lg glass text-slate-300 hover:text-white">Copy</button>
        <button onclick="shareLink()" title="Copy a shareable link to this peptide" class="text-xs px-2 py-1 rounded-lg glass text-slate-300 hover:text-white flex items-center gap-1"><svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>Copy link</button>
        ${s.max_engines>1?`<span class="px-2 py-1 rounded-lg text-xs bg-plum/20 text-plum font-semibold">${s.max_engines}-engine consensus</span>`:''}
        ${s.has_im?`<span class="px-2 py-1 rounded-lg text-xs bg-teal/20 text-teal">ion mobility</span>`:''}
      </div>
      <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-4 mt-5">
        ${stat('Precursors',fmt(s.n_precursors))}${stat('Mod-forms',fmt(s.n_modforms))}${stat('Charges',fmt(s.n_charges))}
        ${stat('Runs',fmt(s.n_runs))}${stat('Best q-value',sci(s.best_q_value))}${stat('Avg 1/K₀',fmtF(s.avg_im,3))}
      </div>
    </div>
    <div id="sumbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-20 rounded-xl"></div></div>
    <div id="flybox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-16 rounded-xl"></div></div>
    <div id="predbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-48 rounded-xl"></div></div>
    <div id="xicbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-64 rounded-xl"></div></div>
    <div id="interfbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-32 rounded-xl"></div></div>
    <div id="lcabox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-20 rounded-xl"></div></div>
    <div id="protbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-24 rounded-xl"></div></div>
    <div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-3">Modified forms × charge</h3>
    ${table(['ProForma','Charge','Obs','Avg m/z','Avg RT','Avg 1/K₀','Best q','Avg log₂ int','Engines'],
      d.forms.map(f=>[`<span class="font-mono text-xs text-white break-all">${esc(f.modified_seq_proforma||'—')}</span>`,f.charge+'+',fmt(f.n_obs),fmtF(f.avg_mz,4),fmtF(f.avg_rt),fmtF(f.avg_im,3),sci(f.best_q_value),fmtF(f.avg_log2_int),f.max_engines>1?`${f.max_engines}×`:'1']))}</div>
    <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-3">Observations across runs (${d.observations.length})</h3>
    ${table(['Search','Engine','Run','Charge','m/z','RT','1/K₀','q-value','Intensity'],
      d.observations.map(o=>[`<span class="text-accent-400 cursor-pointer" onclick="event.stopPropagation();go('run','${o.search_id}')">${esc(o.search_name||'—')}</span>`,esc(o.search_engine||'—'),`<span class="font-mono text-[11px]">${esc((o.raw_path||'').split('/').pop())}</span>`,o.charge+'+',fmtF(o.precursor_mz,4),fmtF(o.rt),fmtF(o.im,3),sci(o.q_value),sci(o.intensity)])) }</div>`;
    loadSummary(seq); loadFlyability(seq); loadPredicted(seq, 2); loadXIC(seq); loadInterference(seq); loadLCA(seq); loadProteins(seq);
  }catch(e){ dbError(e); }
}

async function loadFlyability(seq){
  const el=$('#flybox'); if(!el)return;
  try{
    const d=await api(`/api/peptide/${encodeURIComponent(seq)}/flyability`);
    if(d.flyability==null){ el.innerHTML=`<h3 class="font-bold text-white mb-1">Predicted flyability <span class="text-[10px] text-slate-500 font-normal">Koina PFly</span></h3>${empty('Flyability prediction unavailable for this peptide.')}`; return; }
    const pct=Math.round(d.flyability*100);
    const tier = d.flyability>=0.66?['Strong flyer','text-emerald-300','#34d399'] : d.flyability>=0.33?['Moderate flyer','text-amber-300','#FFBF00'] : ['Poor flyer','text-rose-300','#fb7185'];
    const cls=(d.classes||[]).map(p=>p==null?0:p);
    const lab=['1 (poor)','2','3','4 (strong)'];
    const bars=cls.map((p,i)=>`<div class="flex items-center gap-2 text-[11px]"><span class="w-16 text-slate-400">class ${lab[i]}</span><div class="flex-1 bg-white/5 rounded h-2 overflow-hidden"><div class="h-2 rounded" style="width:${Math.round(p*100)}%;background:${tier[2]}"></div></div><span class="w-9 text-right text-slate-400">${Math.round(p*100)}%</span></div>`).join('');
    el.innerHTML=`<h3 class="font-bold text-white mb-1">Predicted flyability <span class="text-[10px] text-slate-500 font-normal">Koina PFly · ${esc(d.model||'pfly_2024_fine_tuned')}</span></h3>
      <p class="text-[11px] text-slate-500 mb-3">How readily this peptide ionizes and is detected by MS — sequence-intrinsic (charge-independent). 0 = poor flyer, 1 = strong flyer. PFly returns a 4-class probability; the headline score is the expected class mapped to 0–1.</p>
      <div class="flex items-center gap-6 flex-wrap">
        <div class="text-center"><div class="text-3xl font-extrabold ${tier[1]} kpi-num">${pct}<span class="text-base">%</span></div><div class="text-[11px] ${tier[1]} font-semibold">${tier[0]}</div></div>
        <div class="flex-1 min-w-[220px] space-y-1">${bars}</div>
      </div>
      <p class="text-[10px] text-slate-600 mt-2">${d.source==='live'?'Computed live just now.':'From the precomputed corpus flyability table.'}</p>`;
  }catch(e){ const el=$('#flybox'); if(el) el.innerHTML=`<h3 class="font-bold text-white mb-1">Predicted flyability</h3>${empty('Unavailable: '+esc(e.message))}`; }
}

async function loadInterference(seq){
  const el=$('#interfbox'); if(!el)return;
  try{
    const d=await api(`/api/peptide/${encodeURIComponent(seq)}/interference`); const x=d.interference;
    if(!x || !x.available){
      el.innerHTML=`<h3 class="font-bold text-white mb-1">Shared transitions &amp; interference <span class="text-[10px] text-slate-500 font-normal">DIA specificity</span></h3>
        ${empty('Once XIC/library data is ingested, this shows other peptides that share these quant-fragment m/z values — co-eluting ones are potential interference.')}`;
      return;
    }
    const tr=(x.transitions||[]).map(t=>`<div class="flex items-center gap-2 text-[11px] py-0.5">
        <span class="font-mono w-14 text-slate-300">${esc(t.label)}</span>
        <span class="text-slate-500 w-20">m/z ${fmtF(t.mz,3)}</span>
        <span class="flex-1">${t.n_sharing} peptides share · <span class="${t.n_co_eluting>0?'text-amber-300':'text-emerald-300'}">${t.n_co_eluting} co-elute</span></span></div>`).join('');
    const partners=(x.partners||[]).filter(p=>p.co_eluting>0).slice(0,24).map(p=>`
        <tr class="row-hover border-b border-white/5 cursor-pointer" onclick="go('peptide','${encodeURIComponent(p.peptide)}')">
          <td class="py-1.5 px-2 font-mono text-accent-400">${esc(p.peptide)}</td>
          <td class="py-1.5 px-2 text-slate-300">${p.shared}</td>
          <td class="py-1.5 px-2 text-amber-300">${p.co_eluting}</td>
          <td class="py-1.5 px-2 text-slate-400">${p.min_dRT!=null?fmtF(p.min_dRT,2)+' min':'—'}</td></tr>`).join('');
    el.innerHTML=`
      <h3 class="font-bold text-white mb-1">Shared transitions &amp; interference <span class="text-[10px] text-slate-500 font-normal">other peptides sharing this one's quant fragments</span></h3>
      <p class="text-[11px] text-slate-500 mb-3">A transition shared by a <span class="text-amber-300">co-eluting</span> peptide (|ΔRT| ≤ ${x.rt_window} min, ±${x.mz_tol} m/z) is potential interference; RT-resolved sharing is fine. Helps judge each transition's specificity.</p>
      <div class="grid lg:grid-cols-2 gap-5">
        <div><div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">This peptide's quant transitions</div>${tr||empty('—')}</div>
        <div><div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Co-eluting peptides sharing a transition</div>
          ${partners?`<div class="overflow-x-auto"><table class="w-full text-xs"><thead><tr class="text-left text-[10px] uppercase text-slate-500 border-b border-white/10"><th class="py-1 px-2">Peptide</th><th class="py-1 px-2">Shared</th><th class="py-1 px-2">Co-elute</th><th class="py-1 px-2">min ΔRT</th></tr></thead><tbody>${partners}</tbody></table></div>`:empty('No co-eluting interferers — transitions look specific.')}</div>
      </div>`;
  }catch(e){ const el=$('#interfbox'); if(el) el.innerHTML=`<h3 class="font-bold text-white mb-1">Shared transitions &amp; interference</h3>${empty('Unavailable: '+esc(e.message))}`; }
}

async function loadPredicted(seq, charge){
  const el=$('#predbox'); if(!el)return;
  try{
    const d=await api(`/api/peptide/${encodeURIComponent(seq)}/predicted?charge=${charge}`); const p=d.predicted;
    const names=p.model_names||[];
    if(!names.length){ el.innerHTML=`<h3 class="font-bold text-white mb-1">Predicted fragment intensities <span class="text-[10px] text-slate-500 font-normal">Koina</span></h3>${empty('Koina prediction service unavailable right now.')}`; return; }
    const MCOL={'Prosit_2020_intensity_HCD':'#00B5E2','AlphaPeptDeep_ms2_generic':'#FFBF00','ms2pip_HCD2021':'#6CCA98'};
    // stick/stem spectrum: each peak is a vertical line at its m/z (0 -> intensity), NaN breaks between
    const cap=a=>(a||[]).slice(0,24);
    function stems(peaks,isAvg){ const a=[]; cap(peaks).forEach(f=>{ a.push({x:f.mz,y:0});
      const pt={x:f.mz,y:f.rel_intensity,ion:f.ion||f.label}; if(isAvg)pt.agree=f.n_models_agree; a.push(pt);
      a.push({x:f.mz,y:null}); }); return a; }
    const ds=names.map(n=>({label:n.replace(/_/g,' '),data:stems(p.models[n]),borderColor:(MCOL[n]||'#9aa')+'cc',
      borderWidth:1.4,pointRadius:0,spanGaps:false,tension:0,fill:false}));
    ds.push({label:'average',data:stems(p.average,true),borderColor:'#ffffff',borderWidth:2.5,pointRadius:0,spanGaps:false,tension:0,fill:false});
    // the search's OWN library intensities (DIA-NN), overlaid when ingested
    const sl=d.search_library;
    if(sl && sl.peaks && sl.peaks.length){
      ds.push({label:`DIA-NN ${sl.version||''} (this search)`.replace('  ',' '),data:stems(sl.peaks),
        borderColor:'#ff5cf0',borderWidth:2,pointRadius:0,spanGaps:false,tension:0,fill:false,borderDash:[3,2]});
    }
    el.innerHTML=`
      <div class="flex items-center justify-between flex-wrap gap-2 mb-1">
        <h3 class="font-bold text-white">Predicted fragment intensities <span class="text-[10px] text-slate-500 font-normal">Koina · ${names.length} models</span></h3>
        <div class="flex items-center gap-2 text-[11px]">
          <span class="text-slate-500">charge</span>
          ${[2,3,4].map(z=>`<button onclick="loadPredicted('${encodeURIComponent(seq)}',${z})" class="px-2 py-0.5 rounded ${z===charge?'tab-active':'glass text-slate-300'}">+${z}</button>`).join('')}
        </div>
      </div>
      <p class="text-[11px] text-slate-500 mb-2">Independent ML predictors (${names.map(n=>esc(n.replace(/_/g,' '))).join(', ')}) at charge +${charge}, CE≈${p.ce}. White line = across-model average. These are <span class="text-slate-400">predicted</span> — your search's own library intensities overlay here once <code>report-lib</code> is ingested.</p>
      <div class="h-56"><canvas id="c_pred"></canvas></div>`;
    const cc=chartColors();
    charts.c_pred=new Chart($('#c_pred'),{type:'line',data:{datasets:ds},
      options:{interaction:{mode:'nearest',intersect:true},
        plugins:{legend:{position:'bottom',labels:{color:cc.tick,boxWidth:8,font:{size:10}}},
          tooltip:{filter:i=>i.raw&&i.raw.ion,callbacks:{
            title:items=>items[0]?'m/z '+fmtF(items[0].parsed.x,4):'',
            label:c=>`${c.dataset.label}: ${c.raw.ion} (${fmtF(c.parsed.y,3)})${c.raw.agree!=null?` · ${c.raw.agree}/${names.length} models`:''}`}}},
        scales:{x:{type:'linear',title:{display:true,text:'m/z',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}},
          y:{min:0,max:1.05,title:{display:true,text:'relative intensity',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}}},
        maintainAspectRatio:false}});
  }catch(e){ const el=$('#predbox'); if(el) el.innerHTML=`<h3 class="font-bold text-white mb-1">Predicted fragment intensities</h3>${empty('Koina unavailable: '+esc(e.message))}`; }
}

async function loadFragments(seq){
  const el=$('#fragbox'); if(!el)return;
  try{
    const d=await api(`/api/peptide/${encodeURIComponent(seq)}/fragments`); const f=d.fragments;
    if(!f || !f.ions || !f.ions.length){ el.remove(); return; }
    const cc=chartColors();
    const col=t=>t==='b'?'#00B5E2':'#FF6B6B';  // b = cyan, y = red
    const ds=['b','y'].map(t=>({label:t+' ions',data:f.ions.filter(i=>i.type===t&&i.charge===1).map(i=>({x:i.mz,y:1,ion:i.ion})),
      backgroundColor:col(t),borderColor:col(t),barThickness:2}));
    el.innerHTML=`
      <div class="flex items-center justify-between flex-wrap gap-2 mb-1">
        <h3 class="font-bold text-white">Theoretical fragment ions (b/y) <span class="text-[10px] text-slate-500 font-normal">computed from sequence · monoisotopic</span></h3>
        <span class="text-[10px] text-slate-500">${f.carbamidomethyl?'Cys +57.02 (carbamidomethyl)':'Cys unmodified'}</span>
      </div>
      <p class="text-[11px] text-slate-500 mb-2">Stick positions are exact m/z; heights are uniform — real intensities and the engine's quant fragments overlay here once the DIA-NN spectral library is ingested.</p>
      <div class="h-40"><canvas id="c_frag_theo"></canvas></div>`;
    charts.c_frag_theo=new Chart($('#c_frag_theo'),{type:'bar',data:{datasets:ds},
      options:{plugins:{legend:{position:'bottom',labels:{color:cc.tick,boxWidth:8,font:{size:10}}},
        tooltip:{callbacks:{label:c=>`${c.raw.ion}  m/z ${fmtF(c.raw.x,4)}`}}},
        scales:{x:{type:'linear',title:{display:true,text:'m/z',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}},
          y:{display:false,min:0,max:1.2}},maintainAspectRatio:false}});
  }catch(e){ const el=$('#fragbox'); if(el) el.remove(); }
}

let _xic=null;
// synthesize a peak-shaped trace on the apex-relative grid (-0.5..0.5 min), scaled by
// relative intensity — used only when no real chromatogram exists. Clearly labeled.
function _synthTrace(rel){ const a=[],s=0.06; for(let k=0;k<41;k++){const x=-0.5+k/40; a.push({rt:+x.toFixed(3),i:rel*Math.exp(-(x*x)/(2*s*s))});} return a; }
async function loadXIC(seq){
  const el=$('#xicbox'); if(!el)return;
  try{
    const d=await api(`/api/peptide/${encodeURIComponent(seq)}/xic`); const x=d.xic;
    if(!x || !x.available){
      el.innerHTML=`<h3 class="font-bold text-white mb-1">Extracted-ion chromatogram (XIC) <span class="text-[10px] text-slate-500 font-normal">DIA · the GPMDB-can't-show view</span></h3>
        ${empty('Per-precursor dual-pane XIC (MS1 on top, quantifying fragments below) appears here once a search ingested with DIA-NN --xic / spectral library covers this peptide.')}`;
      return;
    }
    _xic=x;
    const cd=(x.charge_distribution||[]).map(c=>`<span class="px-2 py-0.5 rounded text-[11px] bg-white/5 text-slate-300">+${c.charge} <span class="text-accent-400 font-semibold">${c.pct}%</span> <span class="text-slate-500">(${fmt(c.n_obs)})</span></span>`).join(' ');
    const btns=x.precursors.map((p,i)=>`<button id="xicp_${i}" onclick="selectXICPrec(${i})" class="px-3 py-1 rounded-lg text-xs glass text-slate-300">+${p.charge}${p.has_real_trace?'':' <span class="opacity-60">(predicted)</span>'}</button>`).join('');
    el.innerHTML=`
      <div class="flex items-center justify-between flex-wrap gap-2 mb-1">
        <h3 class="font-bold text-white">Extracted-ion chromatogram (XIC) <span class="text-[10px] text-slate-500 font-normal">per precursor · DIA</span></h3>
      </div>
      <div class="flex flex-wrap items-center gap-2 mb-3 text-[11px]"><span class="text-slate-500">Charge states seen:</span>${cd||'—'}</div>
      <div class="flex flex-wrap gap-2 mb-3">${btns}</div>
      <div id="xicpane"></div>`;
    selectXICPrec(0);
  }catch(e){ el.innerHTML=`<h3 class="font-bold text-white mb-2">Extracted-ion chromatogram (XIC)</h3>${empty('XIC unavailable: '+esc(e.message))}`; }
}
function selectXICPrec(i){
  const x=_xic; if(!x)return; const p=x.precursors[i]; if(!p)return;
  x.precursors.forEach((_,j)=>{const b=$('#xicp_'+j); if(b) b.className='px-3 py-1 rounded-lg text-xs '+(j===i?'tab-active':'glass text-slate-300');});
  ['xic_ms1','xic_frag','xic_mirror'].forEach(id=>{ if(charts[id]){try{charts[id].destroy()}catch(e){} delete charts[id];} });
  const synthetic=!p.has_real_trace;
  const usage=(p.fragment_usage||[]).slice(0,12).map(u=>`<div class="flex items-center gap-2 text-[11px] py-0.5">
      <span class="font-mono w-12 text-slate-300">${esc(u.label)}</span>
      <div class="flex-1 h-2 rounded bg-white/5 overflow-hidden"><div style="width:${u.pct}%;height:100%;background:#FFBF00"></div></div>
      <span class="text-slate-500 w-9 text-right">${u.pct}%</span></div>`).join('');
    $('#xicpane').innerHTML=`
      <div class="text-[11px] mb-2 ${synthetic?'text-amber-300':'text-slate-500'}">${synthetic
        ? '⚠ Predicted XIC — fragment identities &amp; relative intensities are real (spectral library), the elution peak shape is modeled (synthetic time axis). Not a measured chromatogram.'
        : `Real acquired XIC — apex-aligned <b>average</b> of the acquired runs (each run aligned to its apex, then meaned; axis = ${esc(x.rt_axis||'RT − apex, min')}). Observed in ${fmt(p.n_searches)} search${p.n_searches===1?'':'es'}; pools more acquisitions as additional searches are ingested.`}</div>
      <div class="grid lg:grid-cols-4 gap-4">
        <div class="lg:col-span-3 space-y-2">
          <div><div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">MS1 precursor (+${p.charge})</div><div class="h-24"><canvas id="xic_ms1"></canvas></div></div>
          <div><div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Fragment ions (top ${p.fragments.length} quantified)</div><div class="h-44"><canvas id="xic_frag"></canvas></div></div>
          ${synthetic?'':`<div><div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Predicted (DIA-NN library) ↑ vs acquired (XIC apex) ↓ — mirror</div><div class="h-44"><canvas id="xic_mirror"></canvas></div></div>`}
        </div>
        <div><div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Quant-fragment usage % across searches</div>${usage||empty('—')}</div>
      </div>`;
  const cc=chartColors();
  const opts=(xt)=>({plugins:{legend:{display:false}},maintainAspectRatio:false,
    scales:{x:{type:'linear',title:{display:!!xt,text:xt,color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick,maxTicksLimit:6}},
            y:{grid:{color:cc.grid},ticks:{color:cc.tick,callback:v=>sci(v),maxTicksLimit:4}}},elements:{point:{radius:0}}});
  const ms1data = synthetic ? _synthTrace(1) : (p.ms1||[]);
  charts.xic_ms1=new Chart($('#xic_ms1'),{type:'line',data:{datasets:[{data:ms1data.map(q=>({x:q.rt,y:q.i})),
    borderColor:'#022851',backgroundColor:'#02285133',fill:true,tension:.35,borderWidth:2,borderDash:synthetic?[4,3]:[]}]},options:opts(null)});
  charts.xic_frag=new Chart($('#xic_frag'),{type:'line',data:{datasets:(p.fragments||[]).map((f,j)=>({
    label:f.label,data:(synthetic?_synthTrace(f.rel_intensity||0):(f.trace||[])).map(q=>({x:q.rt,y:q.i})),
    borderColor:PALETTE[j%PALETTE.length],backgroundColor:'transparent',tension:.35,borderWidth:1.8,borderDash:synthetic?[4,3]:[]}))},
    options:{...opts(x.rt_axis||'RT − apex (min)'),plugins:{legend:{position:'bottom',labels:{color:cc.tick,boxWidth:8,font:{size:10}}},
    tooltip:{callbacks:{title:items=>`${synthetic?'Δ':''}RT ${fmtF(items[0].parsed.x)} min`}}}}});
  // mirror plot: predicted (DIA-NN library rel-intensity) up, acquired (XIC peak apex) down,
  // each normalized to its own max so the spectral PATTERNS compare regardless of scale.
  if(!synthetic){
    const frg=(p.fragments||[]).filter(f=>f.mz);
    const apexOf=f=>Math.max(0,...(f.trace||[]).map(t=>t.i));
    const maxRel=Math.max(...frg.map(f=>f.rel_intensity||0),1e-9);
    const maxApex=Math.max(...frg.map(apexOf),1e-9);
    const mstem=(fn)=>{const a=[];frg.forEach(f=>{a.push({x:f.mz,y:0});a.push({x:f.mz,y:fn(f),ion:f.ion||f.label});a.push({x:f.mz,y:null});});return a;};
    charts.xic_mirror=new Chart($('#xic_mirror'),{type:'line',data:{datasets:[
      {label:'predicted (DIA-NN library)',data:mstem(f=>(f.rel_intensity||0)/maxRel),borderColor:'#00B5E2',borderWidth:1.8,pointRadius:0,spanGaps:false,tension:0,fill:false},
      {label:'acquired (XIC apex)',data:mstem(f=>-(apexOf(f)/maxApex)),borderColor:'#FFBF00',borderWidth:1.8,pointRadius:0,spanGaps:false,tension:0,fill:false}]},
      options:{plugins:{legend:{position:'bottom',labels:{color:cc.tick,boxWidth:8,font:{size:10}}},
        tooltip:{filter:it=>it.raw&&it.raw.ion,callbacks:{label:c=>`${c.dataset.label}: ${c.raw.ion} (${fmtF(Math.abs(c.parsed.y),3)})`}}},
        scales:{x:{type:'linear',title:{display:true,text:'m/z',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}},
          y:{min:-1.1,max:1.1,grid:{color:cc.grid},ticks:{color:cc.tick,callback:v=>Math.abs(v).toFixed(1)},
             title:{display:true,text:'predicted ↑   acquired ↓',color:cc.tick}}},
        elements:{point:{radius:0}},maintainAspectRatio:false}});
  }
}

function joinNames(arr, max=3){
  const a=(arr||[]).filter(Boolean).map(esc);
  if(!a.length) return '';
  if(a.length<=max){ return a.length===1?a[0]:a.slice(0,-1).join(', ')+' and '+a[a.length-1]; }
  return a.slice(0,max).join(', ')+` and ${a.length-max} more`;
}
async function loadSummary(seq){
  const el=$('#sumbox'); if(!el)return;
  try{
    const d=await api(`/api/peptide/${encodeURIComponent(seq)}/summary`); const s=d.summary;
    if(!s){ el.remove(); return; }
    const a=s.annotation||{};
    const title=[a.gene,a.protein_name||s.consensus_protein_name].filter(Boolean).map(esc).join(' — ');
    const kw=(a.keywords||[]).slice(0,10).map(k=>`<span class="px-2 py-0.5 rounded text-[10px] bg-accent/15 text-accent-400">${esc(k)}</span>`).join(' ');
    // --- plain-language narrative: how many proteins, what they do, where they live ---
    const b=v=>`<span class="text-white font-semibold">${v}</span>`;
    const sp=joinNames((s.species||[]).map(x=>x.name), 3);
    let story=`This peptide occurs in ${b(fmt(s.n_proteins))} UniProt protein${s.n_proteins===1?'':'s'} across ${b(fmt(s.n_organisms))} organism${s.n_organisms===1?'':'s'}`;
    story += sp?` (${sp}).`:'.';
    // does it span multiple distinct protein families?
    if(s.n_distinct_names>1){
      story += ` They span ${b(fmt(s.n_distinct_names))} different protein types — ${joinNames(s.distinct_protein_names,3)}.`;
    } else if(s.consensus_protein_name){
      story += ` They are ${b(esc(s.consensus_protein_name))}.`;
    }
    if(a.function){ story += ` ${esc(a.function)}`; if(a.enriched_from) story += ` <span class="text-[10px] text-slate-500">(function inferred from reviewed ortholog ${esc(a.enriched_from)}${a.ortholog_protein_name?` — ${esc(a.ortholog_protein_name)}`:''})</span>`; }
    if(a.subcellular&&a.subcellular.length){ story += ` <span class="text-slate-400">Typically located in ${a.subcellular.map(esc).join(', ')}.</span>`; }
    el.innerHTML=`
      <h3 class="font-bold text-white mb-1">What is this? <span class="text-[10px] text-slate-500 font-normal">UniProt · Unipept</span></h3>
      <div class="text-lg font-semibold text-white">${title||esc(s.consensus_protein_name||'—')}</div>
      <p class="text-sm text-slate-300 mt-2 leading-relaxed">${story}</p>
      ${kw?`<div class="flex flex-wrap gap-1.5 mt-3">${kw}</div>`:''}
      <div class="text-[10px] text-slate-500 mt-2">${a.accession?`Representative protein ${esc(a.accession)} · `:''}homologue set & taxonomy from Unipept; annotation from UniProt.</div>`;
  }catch(e){ const el=$('#sumbox'); if(el) el.remove(); }
}

async function loadProteins(seq){
  const el=$('#protbox'); if(!el)return;
  try{
    const d=await api(`/api/peptide/${encodeURIComponent(seq)}/proteins`); const p=d.proteins;
    if(!p){ el.innerHTML=`<h3 class="font-bold text-white mb-2">Proteins containing this peptide</h3>${empty('No Unipept hits (not in UniProt, or service unavailable).')}`; return; }
    const orgs=(p.organisms||[]).map(o=>`
      <div class="flex items-start gap-3 py-1.5 border-b border-white/5">
        <div class="min-w-0 flex-1"><span class="text-accent-400 font-medium">${esc(o.organism)}</span>
          <span class="text-[11px] text-slate-500 ml-1">${fmt(o.n)} protein${o.n>1?'s':''}</span>
          <div class="text-[11px] text-slate-400 font-mono mt-0.5 break-all">${(o.proteins||[]).slice(0,6).map(pr=>esc(pr.uniprot_id)).join(', ')}${o.n>6?' …':''}</div></div>
      </div>`).join('');
    el.innerHTML=`
      <div class="flex items-center justify-between flex-wrap gap-2 mb-2">
        <h3 class="font-bold text-white">Proteins containing this peptide <span class="text-[10px] text-slate-500 font-normal">incl. homologues · Unipept</span></h3>
        <div class="text-right"><span class="text-xl font-extrabold text-accent-400">${fmt(p.total_proteins)}${p.capped?'+':''}</span>
          <span class="text-[10px] text-slate-500 ml-1">proteins · ${fmt(p.n_organisms)} organisms</span></div>
      </div>
      <div class="max-h-72 overflow-y-auto">${orgs||empty('—')}</div>
      <div class="text-[10px] text-slate-500 mt-2">All UniProt proteins (across taxa) whose sequence contains this tryptic peptide — i.e. the homologue set. I/L equated.${p.capped?' Capped for display.':''}</div>`;
  }catch(e){ el.innerHTML=`<h3 class="font-bold text-white mb-2">Proteins containing this peptide</h3>${empty('Unavailable: '+esc(e.message))}`; }
}

async function loadLCA(seq){
  const el=$('#lcabox'); if(!el)return;
  try{
    const d=await api(`/api/peptide/${encodeURIComponent(seq)}/lca`); const l=d.lca;
    if(!l){ el.innerHTML=`<h3 class="font-bold text-white mb-2">Taxonomic LCA</h3>${empty('No Unipept LCA for this peptide (not in UniProt, or service unavailable).')}`; return; }
    const lin=(l.lineage||[]).map(x=>`<span class="text-slate-500">${esc(x.rank)}:</span> <span class="${x.name===l.taxon_name?'text-accent-400 font-semibold':'text-slate-300'}">${esc(x.name)}</span>`).join('<span class="text-slate-600 mx-1.5">›</span>');
    el.innerHTML=`
      <div class="flex items-center justify-between flex-wrap gap-2 mb-2">
        <h3 class="font-bold text-white">Taxonomic LCA <span class="text-[10px] text-slate-500 font-normal">Unipept · UniProt-wide</span></h3>
        <div class="text-right"><span class="text-xl font-extrabold text-accent-400">${esc(l.taxon_name||'—')}</span>
          <span class="px-2 py-0.5 ml-1 rounded text-[10px] bg-white/10 text-slate-300">${esc(l.taxon_rank||'')}</span></div>
      </div>
      <div class="text-xs leading-relaxed">${lin||'<span class="text-slate-500">lineage unavailable</span>'}</div>
      <div class="text-[10px] text-slate-500 mt-2">LCA = lowest common ancestor of all UniProt taxa containing this tryptic peptide (I/L equated). Higher rank ⇒ less taxon-specific.</div>`;
  }catch(e){ el.innerHTML=`<h3 class="font-bold text-white mb-2">Taxonomic LCA</h3>${empty('LCA unavailable: '+esc(e.message))}`; }
}

/* ---------- HIGHLIGHTS (leaderboards + word hunt) ---------- */
async function renderHighlights(){
  view.innerHTML=`
    <section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">Corpus Highlights</h1>
    <p class="text-slate-400 text-sm mt-1">Most reproducibly observed peptides, proteins and genes across the corpus — and a bit of fun. (Leaderboards are cached snapshots; first load may take a few seconds.)</p></section>
    <div class="grid lg:grid-cols-3 gap-5 fade-in">
      <div id="lb_pep" class="glass card p-5"><div class="skeleton h-64 rounded-xl"></div></div>
      <div id="lb_prot" class="glass card p-5"><div class="skeleton h-64 rounded-xl"></div></div>
      <div id="lb_gene" class="glass card p-5"><div class="skeleton h-64 rounded-xl"></div></div>
    </div>
    <div id="lb_words" class="glass card p-5 fade-in mt-5"><div class="skeleton h-40 rounded-xl"></div></div>`;
  api('/api/leaderboards?limit=25').then(d=>{
    $('#lb_pep').innerHTML=`<h3 class="font-bold text-white mb-3">🧬 Most common peptides</h3>`+lbTable(
      ['Peptide','Runs','Obs'],(d.peptides||[]).map(r=>[`<span class="font-mono text-xs text-white break-all">${esc(r.stripped_seq)}</span>`,fmt(r.n_runs),fmt(r.n_obs)]),
      (d.peptides||[]).map(r=>`go('peptide','${encodeURIComponent(r.stripped_seq)}')`));
    const sg=[...new Set((d.proteins||[]).map(r=>r.gene).filter(Boolean))].slice(0,25);
    const surl='https://string-db.org/cgi/network?identifiers='+sg.map(encodeURIComponent).join('%0d');
    $('#lb_prot').innerHTML=`<h3 class="font-bold text-white mb-1">🔬 Most common proteins</h3>`
      +(sg.length?`<a href="${surl}" target="_blank" class="text-[11px] text-accent-400 hover:underline">🔗 View as a STRING interaction network ↗</a>`:'')
      +`<div class="mt-2"></div>`+lbTable(
      ['Protein','Gene','Runs'],(d.proteins||[]).map(r=>[`<span class="font-mono text-xs text-white">${esc(r.protein_group)}</span>`,geneCell(r.gene),fmt(r.n_runs)]),
      (d.proteins||[]).map(r=>`go('protein','${encodeURIComponent(r.protein_group)}')`));
    $('#lb_gene').innerHTML=`<h3 class="font-bold text-white mb-3">🧫 Most common genes</h3>`+lbTable(
      ['Gene','Groups','Runs'],(d.genes||[]).map(r=>[`<span class="text-accent-400 hover:underline">${esc(r.gene)}</span>`,fmt(r.n_groups),fmt(r.n_runs)]),
      (d.genes||[]).map(r=>`go('gene','${encodeURIComponent(r.gene)}')`));
  }).catch(e=>{ ['lb_pep','lb_prot','lb_gene'].forEach(id=>{const el=$('#'+id); if(el)el.innerHTML=empty(e.message);}); });
  api('/api/wordhunt').then(d=>{
    const w=d.words||[];
    const badge=c=>({word:'bg-accent/15 text-accent-400',name:'bg-teal/20 text-teal',spicy:'bg-rose-500/20 text-rose-300'}[c]||'bg-white/10 text-slate-300');
    $('#lb_words').innerHTML=`
      <h3 class="font-bold text-white mb-1">🔤 Words hidden in our peptides</h3>
      <p class="text-[11px] text-slate-500 mb-3">Peptides are written in the 20 amino-acid letters (ACDEFGHIKLMNPQRSTVWY), so real words appear in sequences — but no B/J/O/U/X/Z, so the F-word can't occur (no “U”). Prior art: <a class="text-accent-400 hover:underline" href="https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0050039" target="_blank">PLOS One word-decoding</a> · <a class="text-accent-400 hover:underline" href="https://arxiv.org/pdf/1707.08984" target="_blank">protein lipograms (arXiv)</a>.</p>
      <p class="text-[11px] text-slate-500 mb-3">Click a word to see every peptide that contains it — each links on to the proteins it belongs to.</p>
      <div class="flex flex-wrap gap-2">${(w.slice(0,90).map(x=>`<span onclick="go('searchresults','${encodeURIComponent(x.word)}')" class="cursor-pointer px-2.5 py-1 rounded-lg text-xs ${badge(x.category)} hover:ring-1 hover:ring-white/30" title="e.g. ${esc(x.example)} · ${fmt(x.n_peptides)} peptides · ${fmt(x.n_obs)} obs — click to view peptides">${esc(x.word)} <span class="opacity-60">${fmt(x.n_obs)}</span></span>`).join(''))||empty('No words found yet.')}</div>`;
  }).catch(e=>{ const el=$('#lb_words'); if(el)el.innerHTML=empty(e.message); });
}
async function loadFlyabilityScatter(){
  const cc=chartColors(); const ctx=$('#c_fly'); if(!ctx)return;
  try{
    const d=await api('/api/flyability_scatter?n=8000');
    const pts=(d.points||[]).filter(p=>p.flyability!=null && p.mean_log2_intensity!=null);
    if(!pts.length){ ctx.parentElement.innerHTML=empty('Flyability not computed yet — it appears here once the corpus flyability scores are populated.'); return; }
    const col=f=> f>=0.66?'#34d399' : f>=0.33?'#FFBF00' : '#fb7185';
    const data=pts.map(p=>({x:p.flyability,y:p.mean_log2_intensity,seq:p.stripped_seq}));
    if(charts.c_fly) charts.c_fly.destroy();
    charts.c_fly=new Chart(ctx,{type:'scatter',data:{datasets:[{label:'peptides',data,
      backgroundColor:pts.map(p=>col(p.flyability)+'88'),pointRadius:2,pointHoverRadius:5}]},options:{
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>`${c.raw.seq} · flyability ${fmtF(c.parsed.x,2)} · log₂ int ${fmtF(c.parsed.y,1)}`}}},
      onClick:(e,el)=>{ if(el&&el.length){ const dp=data[el[0].index]; if(dp&&dp.seq) go('peptide',encodeURIComponent(dp.seq)); } },
      scales:{x:{min:0,max:1,title:{display:true,text:'Predicted flyability (0 poor → 1 strong)',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}},
              y:{title:{display:true,text:'Mean observed intensity (log₂)',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}}},maintainAspectRatio:false}});
  }catch(e){ const el=$('#c_fly'); if(el) el.parentElement.innerHTML=empty('Unavailable: '+esc(e.message)); }
}

function lbTable(cols, rows, onclicks){
  if(!rows||!rows.length) return empty('No data (query may have timed out — retry shortly).');
  return `<div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="text-left text-[10px] uppercase tracking-wider text-slate-500 border-b border-white/10">${cols.map(c=>`<th class="py-1.5 px-2 font-semibold">${c}</th>`).join('')}</tr></thead><tbody>
  ${rows.map((r,i)=>`<tr class="row-hover border-b border-white/5 ${onclicks?'cursor-pointer':''}" ${onclicks?`onclick="${onclicks[i]}"`:''}>${r.map(c=>`<td class="py-2 px-2 text-slate-300">${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

/* ---------- PROTEIN detail ---------- */
async function renderProtein(pg){
  view.innerHTML=`<div class="skeleton h-64 rounded-xl"></div>`;
  try{ const d=await api(`/api/protein/${encodeURIComponent(pg)}`); const s=d.summary;
    view.innerHTML=`
    ${crumb([['Search','searchresults',pg],['Protein',null]])}
    <div class="glass card p-6 fade-in mb-5">
      <div class="flex items-center gap-3 flex-wrap">
        <h1 class="text-2xl font-extrabold text-white font-mono break-all">${esc(s.protein_group)}</h1>
        <button onclick="copy('${esc(s.protein_group)}')" class="text-xs px-2 py-1 rounded-lg glass text-slate-300 hover:text-white">Copy</button>
        <button onclick="shareLink()" title="Copy a shareable link to this protein" class="text-xs px-2 py-1 rounded-lg glass text-slate-300 hover:text-white flex items-center gap-1"><svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>Copy link</button>
        ${s.gene?`<span onclick="go('gene','${encodeURIComponent(s.gene)}')" title="View gene ${esc(s.gene)}" class="cursor-pointer px-2 py-1 rounded-lg text-xs bg-accent/15 text-accent-400 hover:underline">${esc(s.gene)}</span>`:''}
        ${s.any_contaminant?`<span class="px-2 py-1 rounded-lg text-xs bg-rose-500/15 text-rose-300">contaminant</span>`:''}
      </div>
      <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-5 gap-4 mt-5">
        ${stat('Searches',fmt(s.n_searches))}${stat('Runs',fmt(s.n_runs))}${stat(d.peptides_sequence_mapped?'Peptides (in sequence)':'Peptides (co-observed)',fmt(d.n_mapped_peptides!=null?d.n_mapped_peptides:d.peptides.length))}
        ${stat('Precursors',fmt(s.sum_precursors))}${stat('Best PG q',sci(s.best_pg_q))}
      </div>
    </div>
    <div id="protsumbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-24 rounded-xl"></div></div>
    <div id="covbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-28 rounded-xl"></div></div>
    <div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">Observed peptides (${d.peptides.length})</h3>
    <p class="text-[11px] text-slate-500 mb-3">${d.peptides_sequence_mapped?'Only peptides that map onto this protein’s canonical UniProt sequence (I/L equated) — same set as the coverage map above.':'⚠ Canonical sequence unavailable, so these are peptides co-observed in the same runs and may include co-eluting proteins.'}</p>
    ${table(['Peptide','Pos','Precursors','Charges','Runs','Best q','IM'],
      d.peptides.map(p=>[`<span class="font-mono text-accent-400 hover:underline">${esc(p.stripped_seq)}</span>`,p.start?`<span class="text-[11px] text-slate-500">${p.start}–${p.end}</span>`:'—',fmt(p.n_precursors),fmt(p.n_charges),fmt(p.n_runs),sci(p.best_q_value),p.has_im?'<span class="text-teal">●</span>':'—']),
      d.peptides.map(p=>`go('peptide','${encodeURIComponent(p.stripped_seq)}')`))}</div>
    <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-3">Per search / run (${d.per_search.length})</h3>
    ${table(['Search','Engine','Run','Gene','Unique pep','Precursors','Intensity','PG q'],
      d.per_search.map(r=>[`<span class="text-accent-400 cursor-pointer" onclick="event.stopPropagation();go('run','${r.search_id}')">${esc(r.search_name||'—')}</span>`,esc(r.search_engine||'—'),`<span class="font-mono text-[11px]">${esc((r.raw_path||'').split('/').pop())}</span>`,esc(r.gene||'—'),fmt(r.n_unique_peptides),fmt(r.n_precursors),sci(r.intensity),sci(r.pg_q_value)]))}</div>`;
    loadProtSummary(pg); loadCoverage(pg);
  }catch(e){ dbError(e); }
}

async function loadProtSummary(pg){
  const el=$('#protsumbox'); if(!el)return;
  try{
    const d=await api(`/api/protein/${encodeURIComponent(pg)}/summary`); const s=d.summary;
    if(!s){ el.remove(); return; }
    const a=s.annotation||{}, L=s.links||{}, w=s.wikipedia;
    const kw=(a.keywords||[]).map(k=>`<span class="px-2 py-0.5 rounded text-[10px] bg-accent/15 text-accent-400">${esc(k)}</span>`).join(' ');
    const inter=(a.interactions||[]).slice(0,12).map(g=>`<span class="px-2 py-0.5 rounded text-[10px] bg-teal/15 text-teal">${esc(g)}</span>`).join(' ');
    const links=[L.uniprot&&`<a href="${L.uniprot}" target="_blank" class="text-accent-400 hover:underline">UniProt ↗</a>`,
                 L.ncbi_protein&&`<a href="${L.ncbi_protein}" target="_blank" class="text-accent-400 hover:underline">NCBI ↗</a>`,
                 L.string&&`<a href="${L.string}" target="_blank" class="text-accent-400 hover:underline">STRING network ↗</a>`,
                 (w&&w.url)&&`<a href="${w.url}" target="_blank" class="text-accent-400 hover:underline">Wikipedia ↗</a>`].filter(Boolean).join(' · ');
    el.innerHTML=`
      <div class="flex items-center justify-between flex-wrap gap-2 mb-1"><h3 class="font-bold text-white">About this protein <span class="text-[10px] text-slate-500 font-normal">UniProt + Wikipedia</span></h3><div class="text-xs">${links}</div></div>
      <div class="text-lg font-semibold text-white">${[a.gene,a.protein_name].filter(Boolean).map(esc).join(' — ')||'—'}</div>
      ${a.function?`<p class="text-sm text-slate-300 mt-1"><span class="text-slate-500">Function:</span> ${esc(a.function)}${a.enriched_from?` <span class="text-[10px] text-slate-500">(inferred from reviewed ortholog ${esc(a.enriched_from)}${a.ortholog_protein_name?` — ${esc(a.ortholog_protein_name)}`:''})</span>`:''}</p>`:''}
      ${a.subcellular&&a.subcellular.length?`<p class="text-xs text-slate-400 mt-1"><span class="text-slate-500">Location:</span> ${a.subcellular.map(esc).join(', ')}</p>`:''}
      ${inter?`<p class="text-xs text-slate-500 mt-2">Interacts with:</p><div class="flex flex-wrap gap-1.5 mt-1">${inter}</div>`:''}
      ${kw?`<div class="flex flex-wrap gap-1.5 mt-2">${kw}</div>`:''}
      ${w&&w.extract?`<div class="mt-3 pt-3 border-t border-white/5"><div class="text-[11px] text-slate-500 mb-1">📖 Trivia · ${esc(w.title||'')}</div><p class="text-sm text-slate-300">${esc(w.extract)}</p></div>`:''}`;
  }catch(e){ const el=$('#protsumbox'); if(el) el.remove(); }
}

async function loadCoverage(pg){
  const el=$('#covbox'); if(!el)return;
  try{
    const d=await api(`/api/protein/${encodeURIComponent(pg)}/coverage`);
    if(!d.sequence_available){ el.innerHTML=`<h3 class="font-bold text-white mb-2">Sequence coverage</h3>${empty('Canonical sequence not available from UniProt for '+esc(d.accession)+'.')}`; return; }
    const L=d.length, peps=d.peptides||[];
    const cov=new Array(L).fill(false);
    peps.forEach(p=>{ for(let i=p.start-1;i<p.end&&i<L;i++) cov[i]=true; });
    // overview track — semi-transparent tiles stack -> darker where more peptides overlap (depth)
    const tiles=peps.map(p=>{ const left=100*(p.start-1)/L, w=Math.max(100*(p.end-p.start+1)/L,0.12);
      return `<div title="${esc(p.stripped_seq)} · ${p.start}-${p.end} · ${fmt(p.n_precursors)} prec" style="position:absolute;top:0;height:100%;left:${left}%;width:${w}%;background:#38bdf8;opacity:.45"></div>`; }).join('');
    // highlighted residue sequence (covered = accent)
    let seq=''; for(let i=0;i<L;i++){ seq+= cov[i]?`<span style="background:#0ea5e94d;color:#bae6fd">${d.sequence[i]}</span>`:`<span style="color:#475569">${d.sequence[i]}</span>`; }
    el.innerHTML=`
      <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
        <h3 class="font-bold text-white">Sequence coverage ${d.gene?`<span class="px-2 py-0.5 rounded text-xs bg-accent/15 text-accent-400">${esc(d.gene)}</span>`:''}
          <span class="text-slate-500 text-xs font-normal font-mono">${esc(d.accession)} · ${fmt(L)} aa</span></h3>
        <div class="text-right"><span class="text-2xl font-extrabold text-accent-400">${d.coverage_pct}%</span>
          <span class="text-[10px] text-slate-500 ml-1">${fmt(d.n_mapped)} peptides</span></div>
      </div>
      <div style="position:relative;height:16px;background:#1e293b;border-radius:6px;overflow:hidden">${tiles}</div>
      <div class="mt-3" style="font-family:ui-monospace,monospace;font-size:11px;line-height:1.6;word-break:break-all;letter-spacing:.5px">${seq}</div>`;
  }catch(e){ el.innerHTML=`<h3 class="font-bold text-white mb-2">Sequence coverage</h3>${empty('Coverage unavailable: '+esc(e.message))}`; }
}

/* ---------- GENE detail ---------- */
async function renderGene(gene){
  view.innerHTML=`<div class="skeleton h-64 rounded-xl"></div>`;
  try{ const d=await api(`/api/gene/${encodeURIComponent(gene)}`); const t=d.totals||{};
    view.innerHTML=`
    ${crumb([['Search','searchresults',gene],['Gene',null]])}
    <div class="glass card p-6 fade-in mb-5">
      <div class="flex items-center gap-3 flex-wrap">
        <h1 class="text-2xl font-extrabold text-white">${esc(d.gene)}</h1>
        <span class="px-2 py-1 rounded-lg text-xs bg-accent/15 text-accent-400">gene</span>
        <button onclick="shareLink()" title="Copy a shareable link to this gene" class="text-xs px-2 py-1 rounded-lg glass text-slate-300 hover:text-white flex items-center gap-1"><svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>Copy link</button>
      </div>
      <div class="grid grid-cols-2 sm:grid-cols-3 gap-4 mt-5">
        ${stat('Protein groups',fmt(t.n_groups))}${stat('Searches',fmt(t.n_searches))}${stat('Runs',fmt(t.n_runs))}
      </div>
    </div>
    <div id="genesumbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-24 rounded-xl"></div></div>
    <div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">Proteins for this gene (${d.proteins.length})</h3>
    <p class="text-[11px] text-slate-500 mb-3">Every protein group in the corpus annotated with ${esc(d.gene)} — click through to the protein page.</p>
    ${table(['Protein group','Searches','Runs','Precursors','Unique peptides','Contaminant'],
      d.proteins.map(p=>[`<span class="font-mono text-accent-400 hover:underline">${esc(p.protein_group)}</span>`,fmt(p.n_searches),fmt(p.n_runs),fmt(p.sum_precursors),fmt(p.sum_unique_peptides),p.any_contaminant?'<span class="text-rose-400">yes</span>':'—']),
      d.proteins.map(p=>`go('protein','${encodeURIComponent(p.protein_group)}')`))}</div>
    ${(d.organisms&&d.organisms.length)?`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-3">Where it's seen</h3>
      <div class="flex flex-wrap gap-2">${d.organisms.map(o=>`<span class="px-2.5 py-1 rounded-lg text-xs bg-white/5 text-slate-300">${esc(o.organism)} <span class="text-slate-500">${fmt(o.n_runs)} runs</span></span>`).join('')}</div></div>`:''}
    <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-1">Search pipelines that detected it</h3>
    <p class="text-[11px] text-slate-500 mb-3">Proteogenomics / custom-FASTA searches are flagged — novel or variant evidence for this gene.</p>
    ${table(['Pipeline','Engine','Searches','Type'],
      (d.pipelines||[]).map(p=>[esc(p.pipeline_id||'—'),esc(p.search_engine||'—'),fmt(p.n_searches),p.proteogenomics?'<span class="px-2 py-0.5 rounded text-[10px] bg-plum/20 text-plum">proteogenomics</span>':'<span class="text-slate-500">standard</span>']))}</div>`;
    loadGeneSummary(gene);
  }catch(e){ dbError(e); }
}

async function loadGeneSummary(gene){
  const el=$('#genesumbox'); if(!el)return;
  try{
    const d=await api(`/api/gene/${encodeURIComponent(gene)}/summary`); const s=d.summary;
    if(!s){ el.remove(); return; }
    const a=s.annotation||{}, L=s.links||{}, w=s.wikipedia;
    const kw=(a.keywords||[]).map(k=>`<span class="px-2 py-0.5 rounded text-[10px] bg-accent/15 text-accent-400">${esc(k)}</span>`).join(' ');
    const inter=(a.interactions||[]).slice(0,12).map(g=>`<span onclick="go('gene','${encodeURIComponent(g)}')" class="cursor-pointer px-2 py-0.5 rounded text-[10px] bg-teal/15 text-teal hover:underline">${esc(g)}</span>`).join(' ');
    const link=(href,label)=>href?`<a href="${href}" target="_blank" class="text-accent-400 hover:underline">${label} ↗</a>`:'';
    const links=[link(L.uniprot,'UniProt'),link(L.ncbi_gene,'NCBI Gene'),link(L.genecards,'GeneCards'),link(L.ensembl,'Ensembl'),link(L.string,'STRING'),link(L.protein_atlas,'Protein Atlas'),link(L.gtex,'GTEx'),(w&&w.url)&&link(w.url,'Wikipedia')].filter(Boolean).join(' · ');
    el.innerHTML=`
      <div class="flex items-center justify-between flex-wrap gap-2 mb-1"><h3 class="font-bold text-white">About this gene <span class="text-[10px] text-slate-500 font-normal">UniProt + Wikipedia</span></h3><div class="text-xs">${links}</div></div>
      <div class="text-lg font-semibold text-white">${[a.gene,a.protein_name].filter(Boolean).map(esc).join(' — ')||esc(gene)}</div>
      ${a.function?`<p class="text-sm text-slate-300 mt-1"><span class="text-slate-500">Function:</span> ${esc(a.function)}${a.enriched_from?` <span class="text-[10px] text-slate-500">(reviewed ${esc(a.enriched_from)}${a.ortholog_protein_name?` — ${esc(a.ortholog_protein_name)}`:''})</span>`:''}</p>`:''}
      ${a.subcellular&&a.subcellular.length?`<p class="text-xs text-slate-400 mt-1"><span class="text-slate-500">Location:</span> ${a.subcellular.map(esc).join(', ')}</p>`:''}
      ${inter?`<p class="text-xs text-slate-500 mt-2">Interacts with:</p><div class="flex flex-wrap gap-1.5 mt-1">${inter}</div>`:''}
      ${kw?`<div class="flex flex-wrap gap-1.5 mt-2">${kw}</div>`:''}
      ${w&&w.extract?`<div class="mt-3 pt-3 border-t border-white/5"><div class="text-[11px] text-slate-500 mb-1">📖 ${esc(w.title||'')}</div><p class="text-sm text-slate-300">${esc(w.extract)}</p></div>`:''}`;
  }catch(e){ const el=$('#genesumbox'); if(el) el.remove(); }
}

/* ---------- shared UI bits ---------- */
function stat(label,val){ return `<div><div class="text-[11px] uppercase tracking-wider text-slate-500">${label}</div><div class="text-xl font-bold text-white kpi-num mt-0.5">${val}</div></div>`; }
function table(cols, rows, onclicks){
  return `<div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-white/10">
  ${cols.map(c=>`<th class="py-2 px-3 font-semibold">${c}</th>`).join('')}</tr></thead><tbody>
  ${rows.map((r,i)=>`<tr class="row-hover border-b border-white/5 ${onclicks?'cursor-pointer':''}" ${onclicks?`onclick="${onclicks[i]}"`:''}>
  ${r.map(c=>`<td class="py-2.5 px-3 text-slate-300">${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function crumb(items){ return `<nav class="text-xs text-slate-500 mb-4 flex items-center gap-2">
  ${items.map((it,i)=>{ const [label,vw,p]=it; const last=i===items.length-1;
    return `${i>0?'<span class="text-slate-600">/</span>':''}${vw?`<a onclick="go('${vw}'${p!==undefined?`,'${encodeURIComponent(p)}'`:''})" class="cursor-pointer hover:text-accent-400">${esc(label)}</a>`:`<span class="text-slate-300">${esc(label)}</span>`}`; }).join(' ')}</nav>`; }
function empty(msg){ return `<div class="py-12 text-center text-slate-500"><svg class="mx-auto mb-3 opacity-40" width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M3 7h18M3 12h18M3 17h12"/></svg><div class="text-sm">${esc(msg)}</div></div>`; }
function dbError(e, sel){ const html=`<div class="glass card p-8 text-center fade-in"><div class="w-12 h-12 mx-auto rounded-full bg-amber-500/15 grid place-items-center mb-3"><svg width="24" height="24" fill="none" stroke="#fbbf24" stroke-width="2" viewBox="0 0 24 24"><path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg></div>
  <h3 class="font-bold text-white">Database unavailable</h3><p class="text-sm text-slate-400 mt-1 max-w-md mx-auto">${esc(e.message)}</p>
  <p class="text-xs text-slate-500 mt-3">The browser is wired to the live PG Farm <code>delimp</code> DB. Set the credential (HF Secret <code>DELIMP_PG_PASSWORD</code> or <code>DELIMP_PG_TOKEN_FILE</code>) and reload.</p></div>`;
  if(sel) $(sel).innerHTML=html; else view.innerHTML=html; }

/* nav buttons */
document.querySelectorAll('.navbtn').forEach(b=>b.addEventListener('click',()=>go(b.dataset.view)));

/* internal-view badge (real filenames) when a core-facility key is in use */
if(INTKEY){ const n=document.getElementById('navtabs'); if(n) n.insertAdjacentHTML('beforeend','<span class="ml-2 px-2 py-0.5 rounded text-[10px] bg-accent/20 text-accent-400 font-semibold" title="Core-facility view: real filenames shown">internal</span>'); }

/* boot */
route(); pollHealth(); refreshFooterCounts();
setInterval(pollHealth, 15000);
setInterval(refreshFooterCounts, 30000); // watch it populate
