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
async function api(path){ const r = await fetch(path, INTKEY?{headers:{'X-Internal-Key':INTKEY}}:{}); if(!r.ok){ const e=await r.json().catch(()=>({detail:r.statusText})); const err=new Error(e.detail||('HTTP '+r.status)); err.status=r.status; throw err; } return r.json(); }
function esc(s){ return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
// clickable gene cell inside a row that already has an onclick (stops row navigation)
function geneCell(g){ return g?`<span onclick="event.stopPropagation();go('gene','${encodeURIComponent(g)}')" class="cursor-pointer text-accent-400 hover:underline">${esc(g)}</span>`:'—'; }
function copy(t){ navigator.clipboard.writeText(t); toast('Copied'); }
// Shareable permalink: the hash router makes location.href a stable, openable link
// for the current peptide/protein (like a GPMDB accession URL). Copy it to clipboard.
function shareLink(){
  // location.href is the APP's own URL (the .hf.space deep link with the hash route) even when
  // the app is embedded in the huggingface.co Space wrapper — so this is the correct shareable
  // deep link, which the parent address bar can't show (cross-origin iframe).
  const url=location.href;
  navigator.clipboard.writeText(url).then(
    ()=>toast('Link copied ✓ — paste it in notes or send it; it opens this exact page'),
    ()=>{ window.prompt('Copy this link:', url); });
}
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
    case 'highlights': return renderHighlights(param);
    case 'proteins': return renderProteinsShowcase();
    case 'peptides': return renderPeptidesShowcase();
    case 'allspecies': return renderSpeciesShowcase();
    case 'collaborators': return renderCollaborators();
    case 'mydata': return renderMyData();
    case 'collab': return renderCollaborator(param);
    case 'submission': return renderSubmission(param);
    case 'lab': return renderLab(param);
    case 'peptide': return renderPeptide(param);
    case 'protein': return renderProtein(param);
    case 'gene': return renderGene(param);
    case 'species': return renderSpecies(param);
    case 'searchresults': return renderSearchResults(param);
    case 'run': return renderSearchDetail(param);
    default: return renderDashboard();
  }
}

/* ---------- live status + counts ticker ---------- */
async function pollHealth(){
  try{ const h = await api('/api/health'); const dot=$('#liveDot').firstElementChild;  // /api/health, not /health (edge layer hijacks bare /health on the custom domain)
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
function kpi(label, id, sub, accent, link){ return `
  <div class="glass card p-5 fade-in relative overflow-hidden">
    <div class="absolute -right-6 -top-6 w-24 h-24 rounded-full blur-2xl opacity-30" style="background:${accent}"></div>
    <div class="text-[11px] uppercase tracking-wider text-slate-400">${label}</div>
    <div id="${id}" data-v="0" class="kpi-num text-3xl font-extrabold text-white mt-1">—</div>
    <div class="text-[11px] text-slate-500 mt-1">${sub}</div>
    ${link?`<a onclick="${link.onclick}" class="inline-block mt-2 text-[11px] text-accent-400 cursor-pointer hover:underline">${link.text}</a>`:''}
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
    ${kpi('Distinct peptides','k_pep','unique stripped sequences','#a78bfa',{text:'See top peptides →',onclick:"go('highlights','peptides')"})}
    ${kpi('Protein groups','k_prot','distinct groups','#2dd4bf',{text:'See top proteins →',onclick:"go('highlights','proteins')"})}
    ${kpi('Searches','k_search','ingested search runs','#f472b6')}
  </div>
  <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
    ${kpi('Raw files','k_raw','runs in corpus','#fbbf24')}
    ${kpi('Organisms','k_org','distinct taxa','#34d399',{text:'Browse species →',onclick:"var s=document.getElementById('speciesCard');if(s)s.scrollIntoView({behavior:'smooth'})"})}
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
    <p class="text-[11px] text-slate-500 mb-3">Predicted <b>flyability</b> (how readily a peptide ionizes &amp; is detected — sequence-intrinsic) vs its <b>mean observed precursor intensity</b> across the corpus. Strong flyers should trend higher. Colored by most-likely PFly class (matches the breakdown below).</p>
    <div class="h-80"><canvas id="c_fly"></canvas></div>
    <div id="flySummary" class="mt-4 pt-4 border-t border-white/10"></div>
  </div>

  <div class="grid lg:grid-cols-3 gap-4 mb-6">
    <div id="speciesCard" class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-3">Species <span class="text-[10px] text-slate-500 font-normal">click any to open its page</span></h3><div class="h-44"><canvas id="c_species"></canvas></div><div id="speciesLegend" class="mt-3 space-y-1 max-h-36 overflow-auto pr-1"></div><div id="speciesPending" class="mt-2 text-[10px] text-slate-500"></div></div>
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
    { const pe=$('#speciesPending'); const u=c.unidentified_runs;
      if(pe) pe.textContent = u ? `+ ${fmt(u)} run${u===1?'':'s'} pending species ID (awaiting the DIAMOND-nr predictor)` : ''; }
    drawRecent(d.recent_searches);
    loadIMScatter('c_im', null, 4000);
    loadFlyabilityScatter();
    loadFlyabilitySummary();
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
const COMMON_NAMES={
  'Homo sapiens':'human','Mus musculus':'house mouse','Rattus norvegicus':'rat',
  'Canis lupus familiaris':'dog','Bos taurus':'cattle','Sus scrofa':'pig','Ovis aries':'sheep',
  'Oryctolagus cuniculus':'rabbit','Macaca mulatta':'rhesus macaque','Macaca fascicularis':'crab-eating macaque',
  'Gallus gallus':'chicken','Saccharomyces cerevisiae':"baker's yeast",'Escherichia coli':'E. coli',
  'Oncorhynchus kisutch':'coho salmon','Aspergillus oryzae':'koji mold',
  'Desmodus rotundus':'common vampire bat','Diphylla ecaudata':'hairy-legged vampire bat',
  'Artibeus jamaicensis':'Jamaican fruit bat','Tadarida brasiliensis':'Mexican free-tailed bat',
  'Pteronotus mesoamericanus':'Mesoamerican mustached bat','Eptesicus furinalis':'Argentine brown bat',
  'Molossus nigricans':'black mastiff bat','Lasiurus ega':'southern yellow bat',
  'Noctilio leporinus':'greater bulldog bat','Bauerus dubiaquercus':"Van Gelder's bat",
  'Haemorhous mexicanus':'house finch','Zonotrichia querula':"Harris's sparrow",'Junco hyemalis':'dark-eyed junco',
  // crops / plants
  'Glycine max':'soybean','Triticum aestivum':'bread wheat','Oryza sativa':'rice','Oryza sativa subsp. indica':'rice',
  'Zea mays':'maize','Arabidopsis thaliana':'thale cress','Nicotiana tabacum':'tobacco','Solanum tuberosum':'potato',
  'Solanum lycopersicum':'tomato','Pisum sativum':'pea','Cicer arietinum':'chickpea','Lupinus angustifolius':'narrow-leafed lupin',
  'Cannabis sativa':'cannabis','Gossypium hirsutum':'cotton','Helianthus annuus':'sunflower','Vigna radiata':'mung bean',
  'Vigna radiata var. radiata':'mung bean','Digitaria exilis':'fonio','Cucurbita maxima':'winter squash','Cucurbita moschata':'squash',
  'Hordeum vulgare':'barley','Brassica napus':'rapeseed','Medicago truncatula':'barrel medic','Phaseolus vulgaris':'common bean',
  // fish
  'Oncorhynchus mykiss':'rainbow trout','Salmo salar':'Atlantic salmon','Oreochromis niloticus':'Nile tilapia',
  'Thunnus albacares':'yellowfin tuna','Thunnus thynnus':'Atlantic bluefin tuna','Danio rerio':'zebrafish',
  // other mammals
  'Equus caballus':'horse','Felis catus':'cat','Capra hircus':'goat','Trichechus manatus latirostris':'Florida manatee',
  // fungi / yeast
  'Pichia pastoris':'Pichia yeast','Komagataella phaffii':'Pichia yeast','Komagataella pastoris':'Pichia yeast',
  'Hypocrea jecorina':'Trichoderma reesei','Trichoderma reesei':'Trichoderma reesei','Candida albicans':'thrush yeast',
  'Neurospora crassa':'red bread mold',
  // invertebrates / protozoa
  'Octopus vulgaris':'common octopus','Hypsibius dujardini':'tardigrade','Meloidogyne javanica':'root-knot nematode',
  'Caenorhabditis elegans':'roundworm','Drosophila melanogaster':'fruit fly','Apis mellifera':'honey bee',
  'Toxoplasma gondii':'Toxoplasma','Plasmodium falciparum':'malaria parasite',
  // bacteria
  'Xylella fastidiosa':'Xylella','Bacillus subtilis':'B. subtilis','Pseudomonas aeruginosa':'P. aeruginosa',
  'Staphylococcus aureus':'staph','Salmonella enterica':'Salmonella',
  // filled 2026-06 (were rendering blank on the species page)
  'Xenopus laevis':'African clawed frog','Mesocricetus auratus':'golden hamster',
  'Acipenser ruthenus':'sterlet sturgeon','Aedes aegypti':'yellow-fever mosquito',
  'Aspergillus niger':'black mold','Sorghum bicolor':'sorghum','Populus alba':'white poplar',
  'Vitis vinifera':'grape','Zonotrichia albicollis':'white-throated sparrow',
  'Ectocarpus siliculosus':'brown alga','Thalassiosira pseudonana':'marine diatom',
  'Spironucleus salmonicida':'salmon gut parasite','Edwardsiella piscicida':'fish-pathogen bacterium',
  'Enhygromyxa salina':'marine myxobacterium','Vibrio cholerae':'cholera bacterium',
};
// Strip a trailing parenthetical (e.g. "(strain K12)", "(Human)") before the lookup so
// strain-/common-qualified organism strings still resolve; then fall back to genus+species.
function commonName(sci){
  if(!sci || sci==='Unknown') return '';
  if(COMMON_NAMES[sci]) return COMMON_NAMES[sci];
  const bare=sci.replace(/\s*\([^)]*\)\s*$/,'').trim();
  if(COMMON_NAMES[bare]) return COMMON_NAMES[bare];
  const gs=bare.split(/\s+/).slice(0,2).join(' ');   // genus + species (handles "… serotype/strain …")
  return COMMON_NAMES[gs] || '';
}
function drawSpecies(rows){ const cc=chartColors(); const ctx=$('#c_species'); if(!ctx)return;
  const cols=rows.map((_,i)=>PALETTE[i%PALETTE.length]);
  charts.species=new Chart(ctx,{type:'doughnut',data:{labels:rows.map(r=>r.organism),datasets:[{data:rows.map(r=>r.n_runs),backgroundColor:cols,borderWidth:0}]},
  options:{cutout:'62%',onClick:(e,el)=>{ if(el&&el.length){ const o=rows[el[0].index]; if(o&&o.organism&&o.organism!=='Unknown') go('species',o.organism); } },
    plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>{const cm=commonName(c.label);return `${c.label}${cm?' ('+cm+')':''}: ${c.parsed} runs · click for details`;}}}},maintainAspectRatio:false}});
  // custom legend: scientific name with its common name underneath — click → species page
  const el=$('#speciesLegend'); if(el) el.innerHTML=rows.map((r,i)=>{const cm=commonName(r.organism); const click=r.organism!=='Unknown'?`onclick="go('species','${(r.organism||'').replace(/'/g,"\\'")}')" class="flex items-center gap-2 text-[11px] cursor-pointer hover:bg-white/5 rounded px-1 -mx-1"`:`class="flex items-center gap-2 text-[11px]"`;return `
    <div ${click}>
      <span class="w-2.5 h-2.5 rounded-sm shrink-0" style="background:${cols[i]}"></span>
      <span class="min-w-0 flex-1 leading-tight"><span class="text-slate-200 italic">${esc(r.organism)}</span>${cm?`<span class="block text-slate-500 not-italic">${esc(cm)}</span>`:''}</span>
      <span class="text-slate-500 tabular-nums">${fmt(r.n_runs)}</span>
    </div>`;}).join(''); }
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

// robust percentile of a numeric array (p in 0..1); arr need not be sorted
function pctlOf(arr, p){ const s=arr.filter(v=>v!=null&&isFinite(v)).sort((a,b)=>a-b); if(!s.length) return null;
  const i=(s.length-1)*p, lo=Math.floor(i), hi=Math.ceil(i); return s[lo]+(s[hi]-s[lo])*(i-lo); }
// drop iRT/1-K0 outliers so the main cloud fills the plot — 0.5–99.5 percentile fences.
// axes: 'x' (iRT only, for the histogram) or 'xy' (both, for the scatter).
function imTrim(points, axes){
  const xs=points.map(p=>p.rt), xlo=pctlOf(xs,0.005), xhi=pctlOf(xs,0.995);
  let ylo=null, yhi=null;
  if(axes==='xy'){ const ys=points.map(p=>p.im); ylo=pctlOf(ys,0.005); yhi=pctlOf(ys,0.995); }
  if(xlo==null || xhi==null || xhi<=xlo) return points;          // not enough spread to trim
  return points.filter(p=> p.rt!=null && isFinite(p.rt) && p.rt>=xlo && p.rt<=xhi &&
    (ylo==null || p.im==null || (p.im>=ylo && p.im<=yhi)) );
}

function renderIMChart(canvasId, mode){
  const cc=chartColors(); const ctx=$('#'+canvasId); const d=imData[canvasId]; if(!ctx||!d)return;
  if(charts[canvasId]) charts[canvasId].destroy();
  const xlabel = d.x_axis || 'Retention time (min)';
  if(mode==='hist'){
    // iRT (or RT) × peptide number: bin the sampled distinct precursors, count per bin.
    const xs = imTrim(d.points,'x').map(p=>p.rt).filter(v=>v!=null && isFinite(v));
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
  const byCharge={}; imTrim(d.points,'xy').forEach(p=>{ const z=p.charge||0; (byCharge[z] ||= []).push({x:p.rt,y:p.im}); });
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
        <div class="flex gap-2 items-center"><span class="px-3 py-1 rounded-lg text-xs font-bold ${engBadge(s.search_engine)}">${esc((s.search_engine||'?').toUpperCase())} ${esc(s.search_engine_version||'')}</span>
        <span class="px-3 py-1 rounded-lg text-xs ${statusBadge(s.status)}">${esc(s.status||'?')}</span>
        ${window.__FRAN_TIER__&&window.__FRAN_TIER__!=='public'?`<button onclick="exportReport('${esc(id)}',this,'report')" class="px-3 py-1 rounded-lg text-xs font-semibold bg-accent/20 text-accent-400 hover:bg-accent/30" title="Download a DIA-NN report.parquet for this search, ready to upload into DE-LIMP (LIMPA) on Hugging Face or locally">⬇ report.parquet → DE-LIMP</button>
        <button onclick="exportReport('${esc(id)}',this,'brief')" class="px-3 py-1 rounded-lg text-xs font-semibold bg-plum/20 text-plum hover:bg-plum/30" title="Download a markdown brief (raw-file locations, FASTA to download, conditions) to hand to a HIVE-connected Claude to re-search with DIA-NN + analyze with LIMPA">📝 Re-search this data</button>`:''}</div>
      </div>
      ${window.__FRAN_TIER__&&window.__FRAN_TIER__!=='public'?`<div class="mt-3 text-[11px] text-slate-500"><b>report.parquet</b> → upload into <b>DE-LIMP</b> (Hugging Face or local) to run LIMPA. &nbsp;·&nbsp; <b>HIVE brief</b> → a markdown packet (raw-file paths, FASTA to download, conditions) to give a HIVE-connected Claude to re-search with DIA-NN + analyze with LIMPA.</div>`:''}
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
  <div class="flex gap-2 mt-3 text-sm flex-wrap"><button id="tab_pep" onclick="srTab('pep')" class="px-3 py-1.5 rounded-lg tab-active">Peptides</button>
  <button id="tab_prot" onclick="srTab('prot')" class="px-3 py-1.5 rounded-lg glass text-slate-300">Proteins / genes</button>
  <button id="tab_species" onclick="srTab('species')" class="px-3 py-1.5 rounded-lg glass text-slate-300">Species</button>
  ${window.__FRAN_INTERNAL__?`<button id="tab_people" onclick="srTab('people')" class="px-3 py-1.5 rounded-lg glass text-rose-300" title="PI / submitter / submission number — confidential">🔒 People / Submissions</button>`:''}
  <label class="ml-auto flex items-center gap-2 text-xs text-slate-400"><input type="checkbox" id="exactChk" onchange="srTab(curSrTab)"> exact peptide match</label></div></section>
  <div class="glass card p-4 fade-in" id="srBody"><div class="skeleton h-40 rounded-xl"></div></div>`;
  window._srq=q;
  // Probe all three result types so none is hidden: badge the tabs with counts,
  // and auto-open the first populated one (so a query like "dog" lands on Species,
  // "ALB" on Proteins, and a sequence on Peptides).
  try{
    const [pep,prot,sp,ppl]=await Promise.all([
      api(`/api/search/peptides?q=${encodeURIComponent(q)}&limit=1`).catch(()=>({total:0})),
      api(`/api/search/proteins?q=${encodeURIComponent(q)}&limit=1`).catch(()=>({total:0})),
      api(`/api/search/species?q=${encodeURIComponent(q)}`).catch(()=>({total:0})),
      window.__FRAN_INTERNAL__?api(`/api/internal/people_search?q=${encodeURIComponent(q)}&limit=1`).catch(()=>({total:0})):Promise.resolve({total:0})
    ]);
    const pt=pep.total||0, rt=prot.total||0, st=sp.total||0, lt=ppl.total||0;
    const tp=$('#tab_pep'), tr=$('#tab_prot'), ts=$('#tab_species'), tl=$('#tab_people');
    if(tp) tp.innerHTML=`Peptides${pt?` <span class="opacity-60">(${fmt(pt)})</span>`:''}`;
    if(tr) tr.innerHTML=`Proteins / genes${rt?` <span class="opacity-60">(${fmt(rt)})</span>`:''}`;
    if(ts) ts.innerHTML=`Species${st?` <span class="opacity-60">(${fmt(st)})</span>`:''}`;
    if(tl) tl.innerHTML=`🔒 People / Submissions${lt?` <span class="opacity-60">(${fmt(lt)})</span>`:''}`;
    // if only the People tab has hits (a PI surname / submission number), open it; else usual order
    srTab(lt>0 && pt===0 && rt===0 && st===0 ? 'people'
        : (st>0 && pt===0 ? 'species' : (pt===0 && rt>0 ? 'prot' : 'pep')));
  }catch(e){ srTab('pep'); }
}
let curSrTab='pep';
async function srTab(which){
  curSrTab=which; const q=window._srq;
  $('#tab_pep').className='px-3 py-1.5 rounded-lg '+(which==='pep'?'tab-active':'glass text-slate-300');
  $('#tab_prot').className='px-3 py-1.5 rounded-lg '+(which==='prot'?'tab-active':'glass text-slate-300');
  const tsb=$('#tab_species'); if(tsb) tsb.className='px-3 py-1.5 rounded-lg '+(which==='species'?'tab-active':'glass text-slate-300');
  const tpb=$('#tab_people'); if(tpb) tpb.className='px-3 py-1.5 rounded-lg '+(which==='people'?'tab-active':'glass text-rose-300');
  const body=$('#srBody'); body.innerHTML=`<div class="skeleton h-40 rounded-xl"></div>`;
  try{
    if(which==='people'){
      const d=await api(`/api/internal/people_search?q=${encodeURIComponent(q)}&limit=100`);
      if(!d.rows||!d.rows.length){ body.innerHTML=empty('No PI, submitter, submission number, or institute matches. Try a surname (e.g. "Palczewski") or a submission number.'); return; }
      body.innerHTML=`<div class="text-xs text-slate-500 mb-3">${fmt(d.total)} searches matched by PI · submitter · submission · institute (confidential)</div>`+table(
        ['Search','CoreOmics PI · institute','Submission','Engine','Precursors'],
        d.rows.map(r=>{
          const coPI=[r.pi_first_name,r.pi_last_name].filter(Boolean).join(' ')||[r.submitter_first_name,r.submitter_last_name].filter(Boolean).join(' ')||r.customer_contact||'';
          const who=coPI?`<span class="text-emerald-300 cursor-pointer hover:underline" onclick="event.stopPropagation();go('lab','${esc((coPI||'').replace(/'/g,"\\'"))}')" title="Open lab page">${esc(coPI)}</span>${r.co_institute?`<div class="text-[10px] text-slate-500">${esc(r.co_institute)}</div>`:''}`:(r.path_hint?`<span class="text-slate-400">path: ${esc(r.path_hint)}</span>`:'<span class="text-slate-600">—</span>');
          const subCell=r.coreomics_submission_id?`<span onclick="event.stopPropagation();go('submission','${esc(r.coreomics_submission_id)}')" class="text-accent-400 cursor-pointer hover:underline font-mono text-xs" title="Open submission — all its searches">#${esc(r.coreomics_submission_id)}</span>`:'<span class="text-slate-600">—</span>';
          return [
            `<span class="text-accent-400 cursor-pointer hover:underline" onclick="event.stopPropagation();go('run','${esc(r.search_id)}')">${esc(r.real_search_name||'—')}</span>${r.client?`<div class="text-[10px] text-slate-500">${esc(r.client)}</div>`:''}`,
            who, subCell,
            r.search_engine?esc(r.search_engine):'—', r.n_precursors_total!=null?fmt(r.n_precursors_total):'—'];
        }));
    } else if(which==='species'){
      const d=await api(`/api/search/species?q=${encodeURIComponent(q)}`);
      if(!d.rows.length){ body.innerHTML=empty('No species match. Try a scientific name (e.g. "Canis") or a common name (e.g. "dog", "yeast", "bat").'); return; }
      body.innerHTML = `<div class="text-xs text-slate-500 mb-3">${fmt(d.total)} matching species — click any to open its page</div>`+table(
        ['Species','Common name','Group','Runs','Protein groups'],
        d.rows.map(r=>[`<span class="font-semibold text-accent-400">${esc(r.organism)}</span>`,r.common_name?esc(r.common_name):'—',r.taxon_group&&r.taxon_group!=='Other / unclassified'?esc(r.taxon_group):'—',fmt(r.n_runs),r.n_protein_groups!=null?fmt(r.n_protein_groups):'—']),
        d.rows.map(r=>`go('species','${(r.organism||'').replace(/'/g,"\\'")}')`));
    } else if(which==='pep'){
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
    <div id="funbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-40 rounded-xl"></div></div>
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
    loadFunFacts(seq); loadSummary(seq); loadFlyability(seq); loadPredicted(seq, 2); loadXIC(seq); loadInterference(seq); loadLCA(seq); loadProteins(seq);
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
async function loadFunFacts(seq){
  const el=$('#funbox'); if(!el)return;
  try{
    const d=await api(`/api/peptide/${encodeURIComponent(seq)}/funfacts`); const f=d.funfacts||{};
    const pc=f.physchem||{}, b=f.breadth||{};
    const nOrg=f.n_organisms||0;
    let headline, hclass, emoji;
    if(nOrg>=8){ headline=`A globe-trotting peptide — observed in ${nOrg} species`; hclass='text-emerald-300'; emoji='🌍'; }
    else if(nOrg>=2){ headline=`Observed across ${nOrg} species in this corpus`; hclass='text-teal'; emoji='🧬'; }
    else if(nOrg===1){ headline=`Only observed in ${esc(f.organisms[0].organism)} in this corpus`; hclass='text-amber-300'; emoji='🎯'; }
    else { headline='Observed (species not annotated for these runs)'; hclass='text-slate-300'; emoji='🔬'; }
    const sub=f.found?`Seen ${fmt(b.n_obs)}× across ${fmt(b.n_searches)} search${b.n_searches===1?'':'es'} and ${fmt(b.n_runs)} runs. <span class="text-slate-500">(Where it has been observed in this corpus — the same sequence may also occur in homologous proteins of other species; see “Proteins containing this peptide” below.)</span>`:'Not yet observed in the corpus precursor table.';
    const chips=(f.organisms||[]).slice(0,16).map(o=>`<span class="px-2.5 py-1 rounded-lg text-xs bg-white/5 text-slate-300"><span class="text-accent-400">${esc(o.organism)}</span> <span class="text-slate-500">${fmt(o.n_runs)}</span></span>`).join(' ');
    const resBadge=(r,lab,color)=>(pc.counts&&pc.counts[r]>0)?`<span class="px-2 py-0.5 rounded text-[10px] ${color}">${pc.counts[r]}× ${lab}</span>`:'';
    const resBadges=[resBadge('C','Cys','bg-yellow-500/15 text-yellow-300'),resBadge('W','Trp','bg-indigo-500/15 text-indigo-300'),resBadge('H','His','bg-sky-500/15 text-sky-300'),resBadge('P','Pro','bg-rose-500/15 text-rose-300'),resBadge('M','Met','bg-orange-500/15 text-orange-300')].filter(Boolean).join(' ');
    const gravyTxt=pc.gravy==null?'—':`${fmtF(pc.gravy,2)} <span class="text-[10px] ${pc.hydrophobic?'text-orange-300':'text-sky-300'}">(${pc.hydrophobic?'hydrophobic':'hydrophilic'})</span>`;
    const imTxt=(b.n_im>0&&b.im_min!=null)?`${fmtF(b.im_min,3)} – ${fmtF(b.im_max,3)} <span class="text-[10px] text-slate-500">(${fmt(b.n_im)} obs)</span>`:'—';
    const useIrt=(b.n_irt||0)>0&&b.irt_min!=null;
    const rtTxt=useIrt?`${fmtF(b.irt_min,1)} – ${fmtF(b.irt_max,1)} <span class="text-[10px] text-slate-500">iRT</span>`:(b.rt_min!=null?`${fmtF(b.rt_min,1)} – ${fmtF(b.rt_max,1)} <span class="text-[10px] text-slate-500">min</span>`:'—');
    const mi=f.most_intense;
    const intTxt=mi?`${sci(mi.intensity)} <span class="text-[10px] text-slate-500">in ${esc(mi.organism)} (+${mi.charge})</span>`:'—';
    const chargeTxt=(b.charges&&b.charges.length)?b.charges.map(c=>c+'+').join(', '):'—';
    el.innerHTML=`
      <div class="flex items-center justify-between flex-wrap gap-2 mb-1">
        <h3 class="font-bold text-white">Peptide trading card <span class="text-[10px] text-slate-500 font-normal">fun facts & cool stats — live from the corpus</span></h3></div>
      <div class="rounded-xl p-4 mb-4" style="background:linear-gradient(135deg,rgba(0,181,226,.10),rgba(255,191,0,.08))">
        <div class="text-lg font-extrabold ${hclass}">${emoji} ${esc(headline)}</div>
        <div class="text-sm text-slate-300 mt-0.5">${sub}</div></div>
      <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4 mb-4">
        ${stat('Length',pc.length?`${fmt(pc.length)} aa`:'—')}
        ${stat('Monoisotopic mass',pc.monoisotopic_mass!=null?`${fmtF(pc.monoisotopic_mass,3)} Da`:'—')}
        ${stat('GRAVY',gravyTxt)}
        ${stat('Tryptic?',pc.length?(pc.tryptic?`<span class="text-emerald-300">yes · ends ${esc(pc.c_terminus)}</span>`:`<span class="text-amber-300">no · ends ${esc(pc.c_terminus)}</span>`):'—')}
        ${stat('Species',fmt(nOrg))}
        ${stat('Charge states',chargeTxt)}
        ${stat('Ion mobility 1/K₀',imTxt)}
        ${stat(useIrt?'iRT spread':'RT spread',rtTxt)}
        ${stat('Cross-engine',b.max_engines>1?`<span class="text-plum">${b.max_engines}× engines</span>`:'1 engine')}
        ${stat('Peak abundance',intTxt)}</div>
      ${resBadges?`<div class="mb-3"><div class="text-[11px] uppercase tracking-wider text-slate-500 mb-1.5">Interesting residues</div><div class="flex flex-wrap gap-1.5">${resBadges}</div></div>`:''}
      ${chips?`<div><div class="text-[11px] uppercase tracking-wider text-slate-500 mb-1.5">Seen in these species <span class="text-slate-600">(by run count)</span></div><div class="flex flex-wrap gap-1.5">${chips}</div></div>`:''}
      <div class="text-[10px] text-slate-500 mt-3">Physico-chemical facts are computed from the sequence (monoisotopic residue masses, Kyte–Doolittle GRAVY). Observation counts, species, ion mobility & abundance are aggregated live over every precursor of this exact stripped sequence in FRAN.</div>`;
  }catch(e){ const el=$('#funbox'); if(el) el.innerHTML=`<h3 class="font-bold text-white mb-1">Peptide trading card</h3>${empty('Unavailable: '+esc(e.message))}`; }
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
async function renderHighlights(section){
  view.innerHTML=`
    <section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">Corpus Highlights</h1>
    <p class="text-slate-400 text-sm mt-1">Most reproducibly observed peptides, proteins and genes across the corpus — and a bit of fun. (Leaderboards are cached snapshots; first load may take a few seconds.)</p></section>
    <div class="grid lg:grid-cols-3 gap-5 fade-in">
      <div id="lb_pep" class="glass card p-5"><div class="skeleton h-64 rounded-xl"></div></div>
      <div id="lb_prot" class="glass card p-5"><div class="skeleton h-64 rounded-xl"></div></div>
      <div id="lb_gene" class="glass card p-5"><div class="skeleton h-64 rounded-xl"></div></div>
    </div>
    <div id="lb_words" class="glass card p-5 fade-in mt-5"><div class="skeleton h-40 rounded-xl"></div></div>`;
  if(section){ const id={peptides:'lb_pep',proteins:'lb_prot',genes:'lb_gene'}[section]; const t=id&&$('#'+id);
    if(t){ t.scrollIntoView({behavior:'smooth',block:'center'}); t.classList.add('ring-2','ring-accent-400');
      setTimeout(()=>t.classList.remove('ring-2','ring-accent-400'),2200); } }
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
    _lbWordsData=d.words||[]; _lbWordsN=100; _lbWordsUncommon=false;
    const nC=_lbWordsData.filter(w=>w.common).length;
    $('#lb_words').innerHTML=`
      <h3 class="font-bold text-white mb-1">🔤 Words hidden in our peptides <span class="text-[11px] font-normal text-slate-500">${fmt(nC)} common · ${fmt(_lbWordsData.length)} total</span></h3>
      <p class="text-[11px] text-slate-500 mb-3">A full English dictionary (~50k AA-spellable words) + names + spicy words, hunted across every peptide. No B/J/O/U/X/Z (not amino acids), so the F-word can't occur (no “U”). Prior art: <a class="text-accent-400 hover:underline" href="https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0050039" target="_blank">PLOS One word-decoding</a> · <a class="text-accent-400 hover:underline" href="https://arxiv.org/pdf/1707.08984" target="_blank">protein lipograms (arXiv)</a>. Click a word to see every peptide that contains it.</p>
      <label class="flex items-center gap-2 text-xs text-slate-400 mb-3 cursor-pointer"><input type="checkbox" id="lb_words_uncommon" onchange="lbWordsToggle(this)"> include uncommon / obscure dictionary words</label>
      <div id="lb_words_btns" class="flex flex-wrap gap-2 mb-3"></div>
      <div id="lb_words_chips"></div>`;
    lbWordsShow(100);
  }).catch(e=>{ const el=$('#lb_words'); if(el)el.innerHTML=empty(e.message); });
}
let _lbWordsData=[], _lbWordsN=100, _lbWordsUncommon=false;
function _lbWordsFiltered(){ return _lbWordsUncommon ? _lbWordsData : _lbWordsData.filter(w=>w.common); }
function _lbWordBtn(n,active){ const tot=_lbWordsFiltered().length; return `<button onclick="lbWordsShow(${n})" class="px-2.5 py-1 rounded-lg text-xs ${active?'tab-active':'glass text-slate-300'}">${n>=99999?`All (${fmt(tot)})`:'Top '+n}</button>`; }
function lbWordsToggle(cb){ _lbWordsUncommon=!!cb.checked; lbWordsShow(_lbWordsN); }
function lbWordsShow(n){
  _lbWordsN=n; const el=$('#lb_words_chips'); if(!el)return;
  const list=_lbWordsFiltered();
  const bt=$('#lb_words_btns'); if(bt) bt.innerHTML=[100,250,500].map(k=>_lbWordBtn(k,k===n)).join('')+_lbWordBtn(99999,n>=99999);
  const badge=c=>({word:'bg-accent/15 text-accent-400',name:'bg-teal/20 text-teal',spicy:'bg-rose-500/20 text-rose-300'}[c]||'bg-white/10 text-slate-300');
  const chips=list.slice(0,n).map(x=>`<span onclick="go('searchresults','${encodeURIComponent(x.word)}')" class="cursor-pointer px-2.5 py-1 rounded-lg text-xs ${badge(x.category)} ${x.common?'':'opacity-70'} hover:ring-1 hover:ring-white/30" title="${x.common?'':'(uncommon dictionary word) '}e.g. ${esc(x.example)} · ${fmt(x.n_peptides)} peptides · ${fmt(x.n_obs)} obs — click to view peptides">${esc(x.word)} <span class="opacity-60">${fmt(x.n_obs)}</span></span>`).join('');
  el.innerHTML=`<div class="flex flex-wrap gap-2">${chips||empty('No words at this filter.')}</div>`;
}
async function loadFlyabilityScatter(){
  const cc=chartColors(); const ctx=$('#c_fly'); if(!ctx)return;
  try{
    const d=await api('/api/flyability_scatter?n=8000');
    const pts=(d.points||[]).filter(p=>p.flyability!=null && p.mean_log2_intensity!=null);
    if(!pts.length){ ctx.parentElement.innerHTML=empty('Flyability not computed yet — it appears here once the corpus flyability scores are populated.'); return; }
    // color by the SAME most-likely PFly class (argmax) the breakdown bar uses, so the scatter's
    // color proportions match the bar's percentages (the collapsed 0-1 score bins did NOT).
    const CCOL={4:'#34d399',3:'#facc15',2:'#f97316',1:'#fb7185'};
    const CNAME={4:'strong',3:'intermediate',2:'weak',1:'non-flyer'};
    const col=p=> CCOL[p.klass]||'#94a3b8';
    const data=pts.map(p=>({x:p.flyability,y:p.mean_log2_intensity,seq:p.stripped_seq,klass:p.klass}));
    if(charts.c_fly) charts.c_fly.destroy();
    charts.c_fly=new Chart(ctx,{type:'scatter',data:{datasets:[{label:'peptides',data,
      backgroundColor:pts.map(p=>col(p)+'88'),pointRadius:2,pointHoverRadius:5}]},options:{
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>`${c.raw.seq} · ${CNAME[c.raw.klass]||'?'} flyer · score ${fmtF(c.parsed.x,2)} · log₂ int ${fmtF(c.parsed.y,1)}`}}},
      onClick:(e,el)=>{ if(el&&el.length){ const dp=data[el[0].index]; if(dp&&dp.seq) go('peptide',encodeURIComponent(dp.seq)); } },
      scales:{x:{min:0,max:1,title:{display:true,text:'Predicted flyability (0 poor → 1 strong)',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}},
              y:{title:{display:true,text:'Mean observed intensity (log₂)',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}}},maintainAspectRatio:false}});
  }catch(e){ const el=$('#c_fly'); if(el) el.parentElement.innerHTML=empty('Unavailable: '+esc(e.message)); }
}
async function loadFlyabilitySummary(){
  const el=$('#flySummary'); if(!el)return;
  const COL={4:'#34d399',3:'#facc15',2:'#f97316',1:'#fb7185'};  // strong=green, intermediate=yellow, weak=orange, non-flyer=rose (4 distinct hues)
  try{
    const d=await api('/api/flyability_summary');
    const cats=(d.categories||[]).filter(c=>c.n>0);
    if(!cats.length){ el.innerHTML=''; return; }
    const seg=cats.map(c=>`<div class="h-full flex items-center justify-center text-[10px] font-semibold text-slate-900/80"
        style="width:${c.pct}%;background:${COL[c.klass]}" title="${esc(c.label)}: ${c.n.toLocaleString()} peptides (${c.pct}%)">${c.pct>=7?c.pct+'%':''}</div>`).join('');
    const leg=cats.map(c=>`<span class="inline-flex items-center gap-1.5 mr-3 text-[11px] text-slate-300">
        <span class="inline-block w-2.5 h-2.5 rounded-sm" style="background:${COL[c.klass]}"></span>${esc(c.label)}
        <b class="text-white">${c.pct}%</b><span class="text-slate-500">(${c.n.toLocaleString()})</span></span>`).join('');
    el.innerHTML=`<div class="flex items-center justify-between mb-2"><h4 class="font-semibold text-white text-sm">Most-likely PFly class</h4>
        <span class="text-[10px] text-slate-500">${d.total.toLocaleString()} unique peptides</span></div>
      <div class="flex h-6 rounded-md overflow-hidden mb-2">${seg}</div>
      <div class="flex flex-wrap gap-y-1">${leg}</div>
      <p class="text-[10px] text-slate-600 mt-2">Each peptide counted once, in its single most-probable PFly category (argmax of the 4 class probabilities). Distinct from the continuous flyability score plotted above.</p>`;
  }catch(e){ el.innerHTML=empty('Category breakdown unavailable: '+esc(e.message)); }
}

function lbTable(cols, rows, onclicks){
  if(!rows||!rows.length) return empty('No data (query may have timed out — retry shortly).');
  return `<div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="text-left text-[10px] uppercase tracking-wider text-slate-500 border-b border-white/10">${cols.map(c=>`<th class="py-1.5 px-2 font-semibold">${c}</th>`).join('')}</tr></thead><tbody>
  ${rows.map((r,i)=>`<tr class="row-hover border-b border-white/5 ${onclicks?'cursor-pointer':''}" ${onclicks?`onclick="${onclicks[i]}"`:''}>${r.map(c=>`<td class="py-2 px-2 text-slate-300">${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

/* ---------- PEPTIDES showcase (a fact-filled tour of the corpus' peptides) ---------- */
// clickable peptide chip -> peptide page (stops parent row navigation)
function pepChip(seq){ return `<span onclick="event.stopPropagation();go('peptide','${encodeURIComponent(seq)}')" class="cursor-pointer font-mono text-accent-400 hover:underline break-all">${esc(seq)}</span>`; }

async function renderPeptidesShowcase(){
  view.innerHTML=`
    <section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">🧬 Peptides — a tour of the corpus</h1>
    <p class="text-slate-400 text-sm mt-1">Record-holders, oddballs and hidden words among the corpus' most-observed peptides. Every fact is computed from real data — click any peptide to open its page. (Cached snapshot; first load may take a few seconds.)</p></section>
    <div id="ps_hero" class="glass card p-5 fade-in mb-5"><div class="skeleton h-24 rounded-xl"></div></div>
    <div id="ps_super" class="grid md:grid-cols-2 xl:grid-cols-3 gap-5 fade-in mb-5">
      <div class="glass card p-5 md:col-span-2 xl:col-span-3"><div class="skeleton h-48 rounded-xl"></div></div></div>
    <div class="grid lg:grid-cols-2 gap-5 fade-in mb-5">
      <div id="ps_palindrome" class="glass card p-5"><div class="skeleton h-40 rounded-xl"></div></div>
      <div id="ps_lowalpha" class="glass card p-5"><div class="skeleton h-40 rounded-xl"></div></div></div>
    <div id="ps_lenhist" class="glass card p-5 fade-in mb-5"><div class="skeleton h-56 rounded-xl"></div></div>
    <div id="ps_obshist" class="glass card p-5 fade-in mb-5"><div class="skeleton h-56 rounded-xl"></div></div>
    <div class="grid lg:grid-cols-2 gap-5 fade-in mb-5">
      <div id="ps_mz" class="glass card p-5"><div class="skeleton h-56 rounded-xl"></div></div>
      <div id="ps_im" class="glass card p-5"><div class="skeleton h-56 rounded-xl"></div></div></div>
    <div id="ps_fly" class="glass card p-5 fade-in mb-5"><div class="skeleton h-48 rounded-xl"></div></div>
    <div id="ps_words" class="glass card p-5 fade-in mb-5"><div class="skeleton h-40 rounded-xl"></div></div>
    <div id="ps_poem" class="glass card p-5 fade-in mb-5"></div>
    <div id="ps_code" class="fade-in"></div>`;
  let d;
  try{ d=await api('/api/peptides_showcase'); }
  catch(e){ ['ps_hero','ps_super','ps_palindrome','ps_lowalpha','ps_lenhist','ps_obshist','ps_mz','ps_im','ps_fly','ps_words'].forEach(id=>{const el=$('#'+id); if(el)el.innerHTML=empty('Unavailable: '+esc(e.message));}); return; }
  if(!d || !d.available){ ['ps_hero','ps_super','ps_palindrome','ps_lowalpha','ps_lenhist','ps_obshist','ps_mz','ps_im','ps_fly','ps_words'].forEach(id=>{const el=$('#'+id); if(el)el.innerHTML=empty('Peptide showcase not available yet — it appears once the corpus leaderboard snapshots are built.');}); return; }
  psHero(d.hero, d.pool_size, d.survey_scope);
  psSuperlatives(d.superlatives||{});
  psPalindromes(d.palindromes||[]);
  psLowAlphabet(d.low_alphabet||[]);
  psLengthHist(d.length_hist||[]);
  psObsHist(d.obs_histogram||[]);
  psDistributions(d.distributions||{});
  psFlyers(d.flyers||{}, d.flyability_summary||{});
  psWords(d.words||[]);
  api('/api/weekly_poem').then(d=>psPoem(d.poem)).catch(()=>{ const el=$('#ps_poem'); if(el)el.remove(); });
  api('/api/proteome_code').then(d=>psCode(d)).catch(()=>{ const el=$('#ps_code'); if(el)el.remove(); });
}

function psHero(h, poolSize, scope){
  const el=$('#ps_hero'); if(!el||!h)return;
  const s=scope||"the corpus' most-observed peptides";
  el.innerHTML=`
    <div class="rounded-xl p-4 mb-4" style="background:linear-gradient(135deg,rgba(0,181,226,.10),rgba(255,191,0,.08))">
      <div class="text-lg font-extrabold text-white">A field guide to ${fmt(h.n_peptides)} peptides — ${esc(s)}</div>
      <div class="text-sm text-slate-300 mt-0.5">Superlatives & composition oddities are computed over this set (precomputed offline — never a slow live scan).</div></div>
    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
      ${stat('Peptides surveyed',fmt(h.n_peptides))}
      ${stat('Mean length',h.mean_length!=null?`${fmtF(h.mean_length,1)} aa`:'—')}
      ${stat('Length range',`${fmt(h.min_length)}–${fmt(h.max_length)} aa`)}
      ${stat('Mean mass',h.mean_mass!=null?`${fmt(Math.round(h.mean_mass))} Da`:'—')}
      ${stat('Mean GRAVY',h.mean_gravy!=null?`${fmtF(h.mean_gravy,2)} <span class="text-[10px] ${h.mean_gravy>0?'text-orange-300':'text-sky-300'}">(${h.mean_gravy>0?'hydrophobic':'hydrophilic'})</span>`:'—')}
      ${stat('Tryptic (ends K/R)',h.pct_tryptic!=null?`${fmtF(h.pct_tryptic,1)}%`:'—')}
    </div>
    ${h.n_palindrome?`<div class="text-[11px] text-slate-500 mt-3">Includes <b class="text-white">${fmt(h.n_palindrome)}</b> palindrome peptide${h.n_palindrome===1?'':'s'} (reads the same forwards and backwards) — see below.</div>`:''}`;
}

// A reusable "superlative card": a title + emoji + ranked list of peptides with a metric value.
function psCard(emoji, title, blurb, items, fmtVal){
  const rows=(items||[]).slice(0,8).map((p,i)=>`
    <div class="flex items-center gap-2 py-1 border-b border-white/5 text-sm">
      <span class="w-5 text-right text-slate-600 text-xs">${i+1}</span>
      <span class="flex-1 min-w-0">${pepChip(p.stripped_seq)}</span>
      <span class="text-white font-semibold whitespace-nowrap">${fmtVal(p)}</span>
      <span class="text-[10px] text-slate-500 whitespace-nowrap w-16 text-right" title="observations">${fmt(p.n_obs)}×</span>
    </div>`).join('');
  return `<div class="glass card p-5">
    <h3 class="font-bold text-white mb-0.5">${emoji} ${esc(title)}</h3>
    <p class="text-[11px] text-slate-500 mb-2">${blurb}</p>
    ${rows?`<div>${rows}</div>`:empty('—')}</div>`;
}

function psSuperlatives(s){
  const el=$('#ps_super'); if(!el)return;
  const pct=p=>`${Math.round((p.value||0)*100)}%`;
  const cards=[
    psCard('📏','Longest peptides','By residue count (aa).',s.longest,p=>`${fmt(p.length)} aa`),
    psCard('⚖️','Heaviest peptides','Monoisotopic mass (Da).',s.heaviest,p=>`${fmt(Math.round(p.mass))} Da`),
    psCard('🏆','Most-observed','Total precursor observations across the corpus.',s.most_observed,p=>`${fmt(p.value)}×`),
    psCard('🌐','Seen in most runs','Distinct LC-MS runs this peptide appears in.',s.most_runs,p=>`${fmt(p.value)} runs`),
    psCard('🔋','Most charge states','Distinct precursor charges observed.',s.most_charges,p=>`${fmt(p.value)} z`),
    psCard('🛢️','Most hydrophobic','Highest Kyte–Doolittle GRAVY (sticks to C18).',s.most_hydrophobic,p=>fmtF(p.value,2)),
    psCard('💧','Most hydrophilic','Lowest GRAVY (elutes early, hard to retain).',s.most_hydrophilic,p=>fmtF(p.value,2)),
    psCard('🧪','Most cysteine-rich','Fraction of residues that are Cys (disulfide-prone).',s.most_cys,pct),
    psCard('🟣','Most tryptophan-rich','Fraction of residues that are Trp (rare, UV-active).',s.most_trp,pct),
    psCard('🌀','Most proline-rich','Fraction of residues that are Pro (kinks the backbone).',s.most_pro,pct),
    psCard('🧲','Most histidine-rich','Fraction His — metal-binding, pH-sensitive.',s.most_his,pct),
    psCard('🧈','Most methionine-rich','Fraction Met — oxidation-prone.',s.most_met,pct),
  ];
  el.innerHTML=cards.join('');
}

function psPalindromes(items){
  const el=$('#ps_palindrome'); if(!el)return;
  const chips=(items||[]).map(p=>`<span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs bg-plum/20 hover:ring-1 hover:ring-white/30">${pepChip(p.stripped_seq)} <span class="text-slate-500">${fmt(p.length)}aa</span></span>`).join(' ');
  el.innerHTML=`<h3 class="font-bold text-white mb-0.5">🔁 Palindrome peptides</h3>
    <p class="text-[11px] text-slate-500 mb-3">Sequences that read identically forwards and backwards (e.g. <span class="font-mono">LATAL</span>). A rare structural curiosity — these are the shortest ones in the survey.</p>
    <div class="flex flex-wrap gap-2">${chips||empty('No palindromes in the surveyed set.')}</div>`;
}

function psLowAlphabet(items){
  const el=$('#ps_lowalpha'); if(!el)return;
  const chips=(items||[]).map(p=>`<span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs bg-white/5 hover:ring-1 hover:ring-white/30" title="${esc(p.stripped_seq)} · ${fmt(p.length)} aa from ${p.value} distinct amino acids">${pepChip(p.stripped_seq)} <span class="text-teal">${p.value} AA</span></span>`).join(' ');
  el.innerHTML=`<h3 class="font-bold text-white mb-0.5">🎰 Low-alphabet peptides</h3>
    <p class="text-[11px] text-slate-500 mb-3">Peptides of 6+ residues built from just 2–4 distinct amino acids — minimalist sequences hiding in the data.</p>
    <div class="flex flex-wrap gap-2">${chips||empty('None in the surveyed set.')}</div>`;
}

function psLengthHist(hist){
  const el=$('#ps_lenhist'); if(!el)return;
  if(!hist||!hist.length){ el.innerHTML=`<h3 class="font-bold text-white mb-1">Length distribution</h3>${empty('—')}`; return; }
  el.innerHTML=`<h3 class="font-bold text-white mb-1">📐 Peptide length distribution</h3>
    <p class="text-[11px] text-slate-500 mb-3">How many of the surveyed peptides fall at each length (residues). Tryptic peptides cluster around 8–15 aa.</p>
    <div style="height:240px"><canvas id="ps_lenchart"></canvas></div>`;
  const cc=chartColors(); const ctx=$('#ps_lenchart'); if(!ctx)return;
  if(charts.ps_lenchart) charts.ps_lenchart.destroy();
  charts.ps_lenchart=new Chart(ctx,{type:'bar',data:{labels:hist.map(h=>h.length),
    datasets:[{label:'peptides',data:hist.map(h=>h.n),backgroundColor:'#00B5E2cc',borderRadius:3}]},
    options:{plugins:{legend:{display:false},tooltip:{callbacks:{title:c=>`${c[0].label} residues`,label:c=>`${fmt(c.parsed.y)} peptides`}}},
      scales:{x:{title:{display:true,text:'Length (amino acids)',color:cc.tick},grid:{display:false},ticks:{color:cc.tick,maxTicksLimit:24}},
              y:{title:{display:true,text:'Peptides',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}}},maintainAspectRatio:false}});
}

function psObsHist(hist){
  const el=$('#ps_obshist'); if(!el)return;
  if(!hist||!hist.length){ el.innerHTML=`<h3 class="font-bold text-white mb-1">Detection frequency</h3>${empty('—')}`; return; }
  const total=hist.reduce((s,b)=>s+(b.n_peptides||0),0);
  const oncePct=total?Math.round(100*((hist[0]&&hist[0].lo===1?hist[0].n_peptides:0))/total):0;
  el.innerHTML=`<h3 class="font-bold text-white mb-1">🔁 How often is each peptide seen? (the long tail)</h3>
    <p class="text-[11px] text-slate-500 mb-3">Distinct peptides bucketed by how many times they were observed across the whole corpus (log₂ buckets; log scale). The classic proteomics long tail — ${oncePct}% are seen only a handful of times, a few are seen tens of thousands of times.</p>
    <div style="height:240px"><canvas id="ps_obschart"></canvas></div>`;
  const cc=chartColors(); const ctx=$('#ps_obschart'); if(!ctx)return;
  if(charts.ps_obschart) charts.ps_obschart.destroy();
  charts.ps_obschart=new Chart(ctx,{type:'bar',data:{labels:hist.map(h=>h.label),
    datasets:[{label:'peptides',data:hist.map(h=>h.n_peptides),backgroundColor:'#b07cffcc',borderRadius:3}]},
    options:{plugins:{legend:{display:false},tooltip:{callbacks:{title:c=>`seen ${c[0].label}×`,label:c=>`${fmt(c.parsed.y)} peptides`}}},
      scales:{x:{title:{display:true,text:'Times observed (n_obs)',color:cc.tick},grid:{display:false},ticks:{color:cc.tick}},
              y:{type:'logarithmic',title:{display:true,text:'Peptides (log)',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}}},maintainAspectRatio:false}});
}

function _psHistChart(elId, canvasId, chartKey, title, blurb, xlabel, data, color){
  const el=$('#'+elId); if(!el)return;
  if(!data||!data.length){ el.innerHTML=`<h3 class="font-bold text-white mb-1">${title}</h3>${empty('—')}`; return; }
  el.innerHTML=`<h3 class="font-bold text-white mb-1">${title}</h3>
    <p class="text-[11px] text-slate-500 mb-3">${blurb}</p>
    <div style="height:240px"><canvas id="${canvasId}"></canvas></div>`;
  const cc=chartColors(); const ctx=$('#'+canvasId); if(!ctx)return;
  if(charts[chartKey]) charts[chartKey].destroy();
  charts[chartKey]=new Chart(ctx,{type:'bar',data:{labels:data.map(d=>d.x),
    datasets:[{label:'precursors',data:data.map(d=>d.n),backgroundColor:color,borderRadius:2,barPercentage:1,categoryPercentage:1}]},
    options:{plugins:{legend:{display:false},tooltip:{callbacks:{title:c=>`${xlabel} ≈ ${c[0].label}`,label:c=>`${fmt(c.parsed.y)} precursors`}}},
      scales:{x:{type:'linear',title:{display:true,text:xlabel,color:cc.tick},grid:{display:false},ticks:{color:cc.tick,maxTicksLimit:12}},
              y:{title:{display:true,text:'Precursors',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}}},maintainAspectRatio:false}});
}
function psDistributions(dist){
  _psHistChart('ps_mz','ps_mzchart','ps_mzchart','⚖️ Precursor m/z distribution',
    'Observed precursor mass-to-charge across the corpus (binned over a 20k-precursor sample). Most tryptic precursors land ~400–900 m/z.',
    'm/z', dist.mz||[], '#FFBF00cc');
  _psHistChart('ps_im','ps_imchart','ps_imchart','🌀 Ion mobility distribution',
    'Observed ion mobility (1/K₀, Vs/cm²) for the timsTOF subset of the corpus — the inverse reduced mobility from PASEF runs.',
    '1/K₀', dist.im||[], '#6FCFEBcc');
}

function psFlyers(flyers, summary){
  const el=$('#ps_fly'); if(!el)return;
  const COL={4:'#34d399',3:'#facc15',2:'#f97316',1:'#fb7185'};
  const flyRow=(p,strong)=>`
    <div class="flex items-center gap-2 py-1 border-b border-white/5 text-sm">
      <span class="flex-1 min-w-0">${pepChip(p.stripped_seq)}</span>
      <span class="font-semibold ${strong?'text-emerald-300':'text-rose-300'} whitespace-nowrap">${Math.round((p.flyability||0)*100)}%</span>
      <span class="text-[10px] text-slate-500 whitespace-nowrap w-16 text-right">${fmt(p.n_obs)}×</span></div>`;
  const best=(flyers.best||[]).slice(0,8).map(p=>flyRow(p,true)).join('');
  const worst=(flyers.worst||[]).slice(0,8).map(p=>flyRow(p,false)).join('');
  // 4-class breakdown bar (reuses the same classes/colors as Highlights)
  const cats=(summary.categories||[]).filter(c=>c.n>0);
  const seg=cats.map(c=>`<div class="h-full flex items-center justify-center text-[10px] font-semibold text-slate-900/80" style="width:${c.pct}%;background:${COL[c.klass]}" title="${esc(c.label)}: ${fmt(c.n)} peptides (${c.pct}%)">${c.pct>=8?c.pct+'%':''}</div>`).join('');
  const leg=cats.map(c=>`<span class="inline-flex items-center gap-1.5 mr-3 text-[11px] text-slate-300"><span class="inline-block w-2.5 h-2.5 rounded-sm" style="background:${COL[c.klass]}"></span>${esc(c.label)} <b class="text-white">${c.pct}%</b></span>`).join('');
  el.innerHTML=`
    <h3 class="font-bold text-white mb-0.5">🪰 Flyability extremes <span class="text-[10px] text-slate-500 font-normal">Koina PFly</span></h3>
    <p class="text-[11px] text-slate-500 mb-3">How readily a peptide ionizes and is detected by MS (sequence-intrinsic). The strongest and weakest predicted flyers in the corpus.</p>
    ${(best||worst)?`<div class="grid sm:grid-cols-2 gap-5">
      <div><div class="text-[10px] uppercase tracking-wider text-emerald-300 mb-1">🚀 Strongest flyers</div>${best||empty('—')}</div>
      <div><div class="text-[10px] uppercase tracking-wider text-rose-300 mb-1">🪨 Weakest flyers</div>${worst||empty('—')}</div>
    </div>`:empty('Flyability not computed yet — appears once the corpus flyability table is populated.')}
    ${cats.length?`<div class="mt-4"><div class="flex items-center justify-between mb-1"><div class="text-[10px] uppercase tracking-wider text-slate-500">Most-likely PFly class — whole corpus</div><span class="text-[10px] text-slate-500">${fmt(summary.total)} peptides</span></div>
      <div class="flex h-6 rounded-md overflow-hidden mb-2">${seg}</div><div class="flex flex-wrap gap-y-1">${leg}</div></div>`:''}`;
}

function psCode(d){
  const el=$('#ps_code'); if(!el)return;
  if(!d||!d.available){ el.remove(); return; }
  const fp=d.featured_phrases||[], fpro=d.featured_prophecies||[], totw=d.transmission_of_week;
  const rawPh=d.phrases||[];
  const pg1=x=>(x.protein_group||x.search_id||'').split(';')[0];
  const phChip=x=>`<span onclick="go('peptide','${encodeURIComponent(x.peptide)}')" class="cursor-pointer px-2.5 py-1 rounded-lg text-xs bg-fuchsia-500/15 text-fuchsia-200 hover:ring-1 hover:ring-fuchsia-400/40" title="hidden inside ${esc(x.peptide||'')} — click to verify in the data">${esc((x.text||'').toUpperCase())}</span>`;
  const proRow=x=>{const pg=pg1(x); const lbl=x.search_name?('🔍 '+x.search_name):pg; return `<div class="py-1.5 border-b border-white/5 text-sm"><span class="text-slate-200 italic">${esc(x.text)}</span>${pg?` <span onclick="event.stopPropagation();go('${x.protein_group?'protein':'run'}','${encodeURIComponent(pg)}')" class="cursor-pointer text-[10px] text-fuchsia-300/70 hover:underline">— ${esc(lbl)}</span>`:''}</div>`;};
  el.innerHTML=`
   <div class="glass card p-5 mb-5" style="background:linear-gradient(135deg,rgba(139,92,246,.14),rgba(0,0,0,.25));border:1px solid rgba(139,92,246,.3)">
     <h3 class="font-bold text-white text-lg">🔮 The Proteome Code <span class="text-[11px] font-normal text-fuchsia-300/70">· a FRAN investigation™</span></h3>
     <p class="text-[12px] text-slate-300 mt-1 mb-4">There are <b class="text-fuchsia-200">hidden phrases buried in our search results.</b> Real <b>words</b>, <b>phrases</b>, even entire <b>prophecies</b> — ${fmt(d.n_phrases)} readable phrases plus ${fmt(d.n_prophecies||0)} hand-decoded prophecies, encoded letter-by-letter in the peptides. <b>Who put them there??</b> 👽 Aliens? 🧬 Evolution? 🕵️ The Government? We just decode them — you decide. <span class="text-slate-500">Every word is a literal substring of a real peptide; click any to verify. The signal is in the spectra.</span></p>
     ${totw?`<div class="rounded-xl p-4 mb-4" style="background:rgba(139,92,246,.12);border:1px dashed rgba(139,92,246,.45)">
        <div class="text-[10px] uppercase tracking-widest text-fuchsia-300/80">👁️ Transmission of the week</div>
        <div class="text-2xl font-bold text-fuchsia-100 mt-1 italic">“${esc(totw.text)}”</div>
        <div class="text-[11px] text-slate-400 mt-1">— decoded across the peptides of protein <span onclick="go('protein','${encodeURIComponent(pg1(totw))}')" class="cursor-pointer font-mono text-fuchsia-300 hover:underline">${esc(pg1(totw))}</span>. Rotates weekly. The proteome speaks when it is ready.</div></div>`:''}
     <div class="text-[11px] uppercase tracking-wider text-slate-400 mb-2">🛸 Decoded transmissions <span class="normal-case text-fuchsia-300/60">— hand-verified by FRAN analysts</span></div>
     <div class="flex flex-wrap gap-2 mb-4">${fp.map(phChip).join('')}</div>
     <div class="text-[11px] uppercase tracking-wider text-slate-400 mb-1">📜 The prophecies <span class="normal-case text-slate-500">— messages decoded across a protein's peptides (word order arranged for the truth-seeker)</span></div>
     <div class="rounded-lg mb-3" style="background:rgba(0,0,0,.15)">${fpro.map(proRow).join('')}</div>
     <details class="text-[12px]"><summary class="cursor-pointer text-slate-400 hover:text-slate-200">📡 Open the raw signal — ${fmt(d.n_phrases)} readable phrases the machine found hidden in the peptides</summary>
        <div class="flex flex-wrap gap-2 my-3">${rawPh.slice(0,200).map(phChip).join('')||'<span class="text-slate-600">—</span>'}</div></details>
     <p class="text-[10px] text-slate-600 mt-3">⚠️ Satire. These are real dictionary words/acronyms appearing as substrings of real peptide sequences by chance — protein alphabets use only 20 letters, so words turn up. The decoded transmissions &amp; prophecies have their word order arranged for readability. No actual prophecies were harmed. 🔭</p>
   </div>`;
}
function psPoem(p){
  const el=$('#ps_poem'); if(!el)return;
  if(!p||!p.lines||!p.lines.length){ el.remove(); return; }
  const lineHtml=l=>l.map(t=>t.w
    ? `<span onclick="go('searchresults','${encodeURIComponent((t.t||'').toUpperCase())}')" class="cursor-pointer text-accent-400 hover:underline">${esc(t.t)}</span>`
    : `<span class="text-slate-400">${esc(t.t)}</span>`).join(' ');
  el.innerHTML=`<div class="flex items-baseline justify-between flex-wrap gap-2 mb-1">
      <h3 class="font-bold text-white">🎵 Found poem of the week</h3>
      <span class="text-[11px] text-slate-500">week ${p.week}, ${p.year} · woven from ${fmt(p.n_source_words)} words hidden in the peptides · rotates weekly</span></div>
    <div class="mt-2"><div class="text-accent-400 font-bold text-base mb-2">“${esc(p.title)}”</div>
      <div class="text-lg leading-relaxed italic text-slate-200" style="font-family:Georgia,serif">${p.lines.map(l=>`<div>${lineHtml(l)}</div>`).join('')}</div></div>
    <p class="text-[10px] text-slate-600 mt-3">A computer-assembled “found poem” — every highlighted word is a real word hidden inside the corpus' peptides (click to find them). A new one rotates in each week.</p>`;
}
let _psWordsData=[], _psWordsN=100, _psWordsUncommon=false;
function _psWordsFiltered(){ return _psWordsUncommon ? _psWordsData : _psWordsData.filter(w=>w.common); }
function psWordBtn(n,active){ const tot=_psWordsFiltered().length; return `<button onclick="psWordsShow(${n})" class="px-2.5 py-1 rounded-lg text-xs ${active?'tab-active':'glass text-slate-300'}">${n>=99999?`All (${fmt(tot)})`:'Top '+n}</button>`; }
function psWordsToggleUncommon(cb){ _psWordsUncommon=!!cb.checked; psWordsShow(_psWordsN); }
function psWordsShow(n){
  _psWordsN=n;
  const el=$('#ps_words_chips'); if(!el)return;
  const list=_psWordsFiltered();
  const bt=$('#ps_words_btns'); if(bt) bt.innerHTML=[100,250,500].map(k=>psWordBtn(k,k===n)).join('')+psWordBtn(99999,n>=99999);
  const badge=c=>({word:'bg-accent/15 text-accent-400',name:'bg-teal/20 text-teal',spicy:'bg-rose-500/20 text-rose-300'}[c]||'bg-white/10 text-slate-300');
  const chips=list.slice(0,n).map(x=>`<span onclick="go('searchresults','${encodeURIComponent(x.word)}')" class="cursor-pointer px-2.5 py-1 rounded-lg text-xs ${badge(x.category)} ${x.common?'':'opacity-70'} hover:ring-1 hover:ring-white/30" title="${x.common?'':'(uncommon dictionary word) '}e.g. ${esc(x.example)} · ${fmt(x.n_peptides)} peptides · ${fmt(x.n_obs)} obs — click to find peptides">${esc(x.word)} <span class="opacity-60">${fmt(x.n_obs)}</span></span>`).join(' ');
  el.innerHTML=`<div class="flex flex-wrap gap-2">${chips||empty('No words at this filter.')}</div>`;
}
function psWords(words){
  const el=$('#ps_words'); if(!el)return;
  _psWordsData=words||[]; _psWordsUncommon=false; _psWordsN=100;
  const nCommon=_psWordsData.filter(w=>w.common).length;
  el.innerHTML=`<h3 class="font-bold text-white mb-1">🔤 Words hidden in our peptides <span class="text-[11px] font-normal text-slate-500">${fmt(nCommon)} common · ${fmt(_psWordsData.length)} total</span></h3>
    <p class="text-[11px] text-slate-500 mb-3">A full English dictionary (~50k AA-spellable words) + names + spicy words, hunted across <b>every</b> peptide. Peptides use the 20 amino-acid letters (ACDEFGHIKLMNPQRSTVWY) — so words with B/J/O/U/X/Z can never appear. Sorted by how often they occur (which favors common amino-acid letters). Click a word to find every peptide that contains it.</p>
    <label class="flex items-center gap-2 text-xs text-slate-400 mb-3 cursor-pointer"><input type="checkbox" id="ps_words_uncommon" onchange="psWordsToggleUncommon(this)"> include uncommon / obscure dictionary words</label>
    <div id="ps_words_btns" class="flex flex-wrap gap-2 mb-3"></div>
    <div id="ps_words_chips"></div>`;
  psWordsShow(100);
}

/* ---------- PROTEINS SHOWCASE — a fun, fact-filled tour of the corpus proteins ---------- */
async function renderProteinsShowcase(){
  view.innerHTML=`
    <section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">🧬 The Proteins of FRAN</h1>
    <p class="text-slate-400 text-sm mt-1">A guided tour of every protein in the corpus — the globe-trotters, the whimsically named, the usual contaminant suspects, and the record-breakers. Every number is live from the corpus (cached snapshot; first load may take a few seconds).</p></section>
    <div id="ps_body"><div class="grid md:grid-cols-3 gap-5"><div class="glass card p-5"><div class="skeleton h-40 rounded-xl"></div></div><div class="glass card p-5"><div class="skeleton h-40 rounded-xl"></div></div><div class="glass card p-5"><div class="skeleton h-40 rounded-xl"></div></div></div></div>`;
  try{
    const d=await api('/api/proteins_showcase'); const s=d.showcase||{};
    if(!s.hero){ $('#ps_body').innerHTML=empty('Showcase not ready yet — the protein matview is still being built. Check back shortly.'); return; }
    const H=s.hero;
    const orgPill=o=>o&&o!=='Unknown'?`<span onclick="event.stopPropagation();go('species','${(o||'').replace(/'/g,"\\'")}')" class="cursor-pointer hover:underline text-slate-300" title="${esc(o)}${commonName(o)?` (${esc(commonName(o))})`:''}">${esc(o)}${commonName(o)?` <span class="text-slate-500">${esc(commonName(o))}</span>`:''}</span>`:'<span class="text-slate-500">—</span>';
    const hero=`<div class="glass card p-5 fade-in mb-5"><div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
      ${stat('Protein groups',fmt(H.total_protein_groups))}${stat('Biology (non-contaminant)',fmt(H.n_biology))}${stat('Multi-species',fmt(H.n_multi_species))}
      ${stat('Contaminant groups',fmt(H.n_contaminants))}${stat('Organisms',fmt(H.n_organisms))}${stat('Whimsically named',fmt(H.n_whimsical))}</div></div>`;
    const wt=s.well_traveled||[];
    const descCell=p=>p.blurb?`<span class="text-[11px] text-slate-300">${esc(p.blurb)}</span>`:'<span class="text-slate-600">—</span>';
    const wtRows=wt.map(p=>[`<span class="font-mono text-xs ${p.is_contaminant?'text-rose-300':'text-white'}">${esc(p.protein_group)}</span>${p.is_contaminant?' <span class="text-[9px] px-1 py-0.5 rounded bg-rose-500/15 text-rose-300 align-middle">contaminant</span>':''}`,p.gene?geneCell(p.gene):'—',descCell(p),`<span class="font-bold text-accent-400">${fmt(p.n_species)}</span>`,fmt(p.sum_runs),orgPill(p.top_organism)]);
    const traveled=`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">🌍 Most well-traveled proteins</h3>
      <p class="text-[11px] text-slate-500 mb-3">Detected across the most organisms — the conservation hall of fame. The shared contaminant library (one cRAP FASTA used in every search) genuinely reaches the most species, so the leaders here are flagged; scroll down for the biology-only ranking.</p>
      ${wt.length?lbTable(['Protein','Gene','Description','Species','Runs','Most-seen in'],wtRows,wt.map(p=>`go('protein','${encodeURIComponent(p.protein_group)}')`)):empty('No data yet.')}</div>`;
    const bc=s.biology_conserved||[];
    const bcRows=bc.map(p=>[`<span class="font-mono text-xs text-white">${esc(p.protein_group)}</span>`,p.gene?geneCell(p.gene):'—',descCell(p),`<span class="font-bold text-emerald-300">${fmt(p.n_species)}</span>`,fmt(p.sum_runs),orgPill(p.top_organism)]);
    const conserved=`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">🧬 Most conserved proteins</h3>
      <p class="text-[11px] text-slate-500 mb-3">Same idea, with common lab contaminants filtered out — the proteins seen across the most organisms: deeply-conserved housekeeping machinery.</p>
      ${bc.length?lbTable(['Protein','Gene','Description','Species','Runs','Most-seen in'],bcRows,bc.map(p=>`go('protein','${encodeURIComponent(p.protein_group)}')`)):empty('No multi-species biology yet.')}</div>`;
    const wm=s.whimsical||[];
    const wmCards=wm.map(w=>`<div onclick="go('protein','${encodeURIComponent(w.protein_group)}')" class="cursor-pointer p-4 rounded-xl bg-white/5 border border-plum/30 hover:border-plum/60 hover:bg-white/10 transition">
      <div class="text-lg font-extrabold text-plum">${esc(w.name)}</div>
      <div class="text-[11px] text-slate-500 mb-1">gene <b class="text-slate-300">${esc(w.gene)}</b> · ${esc(w.protein_group)}</div>
      <p class="text-sm text-slate-300">${esc(w.why)}</p>
      <div class="text-[11px] text-slate-500 mt-2">seen in ${fmt(w.sum_runs)} runs${w.top_organism?` · mostly ${esc(w.top_organism)}`:''}</div></div>`).join('');
    const whimsy=wm.length?`<div class="glass card p-5 fade-in mb-5 border border-plum/20"><h3 class="font-bold text-white mb-1">✨ Whimsically named</h3>
      <p class="text-[11px] text-slate-500 mb-3">Biologists name genes with a sense of humor. These delightfully-titled proteins are all present in the corpus — click one to open its page.</p>
      <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">${wmCards}</div></div>`:'';
    const fb=s.function_breakdown||[];
    const funcCard=fb.length?`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">🔬 What kinds of proteins live here?</h3>
      <p class="text-[11px] text-slate-500 mb-3">Every non-contaminant protein group bucketed by function (heuristic, by gene family) — blood, liver, muscle, immune, ribosome, mitochondria, …</p>
      <div class="h-72"><canvas id="ps_func"></canvas></div></div>`:'';
    const cg=s.contaminant_gallery||[];
    const kindColor=k=>({keratin:'bg-amber-500/15 text-amber-300',trypsin:'bg-violet-500/15 text-violet-300',albumin:'bg-sky-500/15 text-sky-300',casein:'bg-emerald-500/15 text-emerald-300'}[k]||'bg-rose-500/15 text-rose-300');
    const cgCards=cg.map(c=>`<div onclick="go('protein','${encodeURIComponent(c.protein_group)}')" class="cursor-pointer p-3 rounded-xl bg-rose-500/5 border border-rose-500/15 hover:bg-rose-500/10 transition">
      <div class="flex items-center justify-between gap-2 mb-1"><span class="font-mono text-xs text-rose-200">${esc(c.protein_group)}</span>
        <span class="text-[9px] px-1.5 py-0.5 rounded ${kindColor(c.kind)} uppercase tracking-wide">${esc(c.kind)}</span></div>
      <p class="text-xs text-rose-100/80 leading-snug">${esc(c.note)}</p>
      <div class="text-[10px] text-slate-500 mt-1.5">${c.gene?`gene ${esc(c.gene)} · `:''}${fmt(c.n_species)} species · ${fmt(c.sum_runs)} runs</div></div>`).join('');
    const contamCard=cg.length?`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">⚠ The usual suspects (contaminant gallery)</h3>
      <p class="text-[11px] text-slate-500 mb-3">${fmt(H.n_contaminants)} contaminant protein groups are tagged. These are the most widespread — keratin from skin & hair, the trypsin you digested with, serum albumin, milk casein. Reagents and tag-alongs, not your biology.</p>
      <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">${cgCards}</div></div>`:'';
    const ma=s.most_abundant||[], mp=s.most_peptides||[];
    const dispName=p=>p.gene||p.protein_group;
    const protCell=(p,label)=>`<span class="font-mono text-accent-400 hover:underline">${esc(label)}</span>`;
    const maRows=ma.map(p=>[protCell(p,dispName(p)),`<span class="font-mono text-emerald-300">${sci(p.peak_mean_int)}</span>`,orgPill(p.top_organism)]);
    const mpRows=mp.map(p=>[protCell(p,dispName(p)),`<span class="font-bold text-accent-400">${fmt(p.max_pep)}</span>`,orgPill(p.top_organism)]);
    const superl=`<div class="grid md:grid-cols-2 gap-4 mb-5">
      <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-1">💪 Most abundant</h3>
        <p class="text-[11px] text-slate-500 mb-3">Highest peak mean intensity reached in any one species — the proteins that shout the loudest.</p>
        ${ma.length?lbTable(['Protein','Peak mean intensity','Where'],maRows,ma.map(p=>`go('protein','${encodeURIComponent(p.protein_group)}')`)):empty('—')}</div>
      <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-1">🧩 Most unique peptides</h3>
        <p class="text-[11px] text-slate-500 mb-3">Most distinct peptides mapped to one protein group — the giants (titin, nebulin) and heavily-covered favorites.</p>
        ${mp.length?lbTable(['Protein','Unique peptides','Where'],mpRows,mp.map(p=>`go('protein','${encodeURIComponent(p.protein_group)}')`)):empty('—')}</div></div>`;
    $('#ps_body').innerHTML=hero+traveled+conserved+whimsy+funcCard+contamCard+superl;
    if(fb.length){ const ctx=$('#ps_func'); if(ctx){ if(charts.ps_func) charts.ps_func.destroy(); const cc=chartColors();
      charts.ps_func=new Chart(ctx,{type:'bar',data:{labels:fb.map(b=>b.label),datasets:[{data:fb.map(b=>b.n),backgroundColor:fb.map((_,i)=>PALETTE[i%PALETTE.length]),borderRadius:6}]},
        options:{indexAxis:'y',plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>`${fmt(c.parsed.x)} protein groups`}}},
          scales:{x:{title:{display:true,text:'protein groups',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick}},y:{grid:{display:false},ticks:{color:cc.tick}}},maintainAspectRatio:false}}); } }
  }catch(e){ const el=$('#ps_body'); if(el) el.innerHTML=empty('Showcase unavailable: '+esc(e.message)); }
}

/* ---------- SPECIES SHOWCASE — a cross-species tour of every organism ---------- */
// clickable species name -> species detail page. Pass the RAW name (NOT
// encodeURIComponent — go() encodes once; double-encoding broke species links).
function spName(name){ return `<span onclick="go('species','${(name||'').replace(/'/g,"\\'")}')" class="cursor-pointer text-accent-400 hover:underline italic">${esc(name)}</span>`; }

async function renderSpeciesShowcase(){
  view.innerHTML=`
    <section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">🌍 The Species of FRAN</h1>
    <p class="text-slate-400 text-sm mt-1">A cross-species tour of every organism in the corpus — the most-sampled, the rare 'seen once' club, the deepest proteomes, and how broadly the corpus spans the tree of life. Every number is live from the corpus — click any species to open its page. (Cached snapshot; first load may take a few seconds.)</p></section>
    <div id="sp_body"><div class="grid md:grid-cols-3 gap-5"><div class="glass card p-5"><div class="skeleton h-40 rounded-xl"></div></div><div class="glass card p-5"><div class="skeleton h-40 rounded-xl"></div></div><div class="glass card p-5"><div class="skeleton h-40 rounded-xl"></div></div></div></div>`;
  try{
    const d=await api('/api/species_showcase'); const s=d.showcase||{};
    if(!s.available || !s.hero){ $('#sp_body').innerHTML=empty('Species showcase not available yet — it appears once organism metadata and the per-species protein matview are built.'); return; }
    const H=s.hero;
    const hero=`<div class="glass card p-5 fade-in mb-5">
      <div class="rounded-xl p-4 mb-4" style="background:linear-gradient(135deg,rgba(0,181,226,.10),rgba(255,191,0,.08))">
        <div class="text-lg font-extrabold text-white">${fmt(H.total_species)} identified species across ${fmt(H.total_runs)} LC-MS runs</div>
        <div class="text-sm text-slate-300 mt-0.5">From human and mouse to vampire bats, salmon and koji mold — every organism whose runs an actual search references.</div></div>
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
        ${stat('Identified species',fmt(H.total_species))}
        ${stat('Runs across them',fmt(H.total_runs))}
        ${stat("Seen in just one run",fmt(H.n_seen_once))}
        ${stat('With a proteome on file',fmt(H.n_with_proteome))}
      </div></div>`;

    // --- most-sampled (by run count) ---
    const ms=s.most_sampled||[];
    const msRows=ms.map(o=>[spName(o.organism),commonName(o.organism)?`<span class="text-slate-400">${esc(commonName(o.organism))}</span>`:'—',`<span class="font-bold text-accent-400">${fmt(o.n_runs)}</span>`,o.organism_taxon_id?`<span class="text-slate-500 text-xs">${esc(o.organism_taxon_id)}</span>`:'—']);
    const sampled=`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">🏆 Most-sampled species</h3>
      <p class="text-[11px] text-slate-500 mb-3">Ranked by the number of distinct LC-MS runs in the corpus — the workhorses of the dataset.</p>
      ${ms.length?lbTable(['Species','Common name','Runs','Taxon ID'],msRows,ms.map(o=>`go('species','${(o.organism||'').replace(/'/g,"\\'")}')`)):empty('No species yet.')}</div>`;

    // --- most protein groups identified ---
    const mp=s.most_proteins||[];
    const pctCell=p=>p&&p.pct!=null?`<span class="font-bold text-teal">${p.pct}%</span> <span class="text-[10px] text-slate-500">${fmt(p.ref_genes)} genes</span>`:'<span class="text-slate-600">—</span>';
    const mpRows=mp.map(o=>[spName(o.organism),commonName(o.organism)?`<span class="text-slate-400">${esc(commonName(o.organism))}</span>`:'—',`<span class="font-bold text-emerald-300">${fmt(o.n_genes)}</span>${o.n_protein_groups!=null?` <span class="text-[10px] text-slate-500">${fmt(o.n_protein_groups)} groups</span>`:''}`,pctCell(o.pct_proteome)]);
    const deepest=`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">🧬 Deepest proteomes</h3>
      <p class="text-[11px] text-slate-500 mb-3">Species with the most distinct <b>genes</b> identified — proteome depth. <b>% of genes</b> = distinct genes ÷ the organism's NCBI protein-coding gene count, <b>summed across all runs of that species</b> (cumulative — not one experiment; this is gene-presence, not isoform/proteoform coverage, which is far lower). The raw protein-<i>group</i> count over-counts (one protein → many group strings across FASTAs).</p>
      ${mp.length?lbTable(['Species','Common name','Distinct genes','% of genes'],mpRows,mp.map(o=>`go('species','${(o.organism||'').replace(/'/g,"\\'")}')`)):empty('No per-species protein counts yet — the matview is still being built.')}</div>`;

    // --- rarest / seen-once club ---
    const rare=s.rarest||[];
    const rareChips=rare.map(o=>{const cm=commonName(o.organism);return `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs bg-white/5 border border-white/10 hover:ring-1 hover:ring-white/30" title="${esc(o.organism)}${cm?` (${esc(cm)})`:''} · ${fmt(o.n_runs)} run${o.n_runs===1?'':'s'}">${spName(o.organism)}${cm?` <span class="text-slate-500 not-italic">${esc(cm)}</span>`:''} <span class="text-amber-300 not-italic">${fmt(o.n_runs)}×</span></span>`;}).join(' ');
    const rareCard=`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">💎 The rare club</h3>
      <p class="text-[11px] text-slate-500 mb-3">The least-sampled organisms — seen in the fewest runs${H.n_seen_once?` (<b class="text-white">${fmt(H.n_seen_once)}</b> appear in just a single run)`:''}. Each is a one-off or near-one-off in the corpus so far.</p>
      <div class="flex flex-wrap gap-2">${rareChips||empty('—')}</div></div>`;

    // --- spotlight species ---
    const sp=s.spotlight;
    const spCm=sp?commonName(sp.organism):'';
    const spotlight=sp?`<div class="glass card p-5 fade-in mb-5 border border-plum/30">
      <h3 class="font-bold text-white mb-1">✨ Spotlight species</h3>
      <p class="text-[11px] text-slate-500 mb-3">The organism with the most protein groups identified in the whole corpus — our most thoroughly-characterized proteome.</p>
      <div class="flex items-baseline gap-3 flex-wrap">
        <div class="text-2xl font-extrabold text-plum italic">${esc(sp.organism)}</div>
        ${spCm?`<div class="text-slate-400">${esc(spCm)}</div>`:''}
        <span class="text-[11px] px-2 py-0.5 rounded bg-white/5 text-slate-400">${esc(sp.taxon_group)} <span class="text-slate-600">(heuristic)</span></span></div>
      <div class="grid grid-cols-2 sm:grid-cols-3 gap-4 mt-4">
        ${stat('Distinct genes',sp.n_genes!=null?fmt(sp.n_genes):'—')}${sp.pct_proteome?stat('Gene coverage',`<span class="text-teal">${sp.pct_proteome.pct}%</span>`):''}${stat('Protein groups',fmt(sp.n_protein_groups))}${stat('Runs',sp.n_runs!=null?fmt(sp.n_runs):'—')}${stat('Taxon ID',sp.organism_taxon_id?esc(sp.organism_taxon_id):'—')}</div>
      ${sp.pct_proteome?`<div class="mt-2 text-[11px] text-slate-500">≈ <b class="text-teal">${sp.pct_proteome.pct}%</b> of protein-coding <b>genes</b> seen at least once, <b>cumulative across all runs</b> (not one experiment): <b>${fmt(sp.n_genes)}</b> of <b>${fmt(sp.pct_proteome.ref_genes)}</b> NCBI genes${sp.pct_proteome.isoform_pct!=null?` · only <b class="text-amber-300">${sp.pct_proteome.isoform_pct}%</b> at isoform level (${fmt(sp.pct_proteome.reviewed_isoforms)} reviewed isoforms)`:''}.</div>`:''}
      <div class="mt-3 text-sm">${spName(sp.organism)} <span class="text-slate-500">— open its page →</span></div></div>`:'';

    // --- taxonomic breadth (heuristic, by genus) ---
    const tb=(s.taxonomic_breadth||[]);
    const breadth=tb.length?`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">🌳 Taxonomic breadth</h3>
      <p class="text-[11px] text-slate-500 mb-3">Two views of how the corpus spreads across broad groups — a <b>heuristic</b> classification by genus (bats, birds, mammals, fish, bacteria, fungi/yeast, plants). Genera we can't confidently place fall into "Other / unclassified".</p>
      <div class="text-[11px] text-slate-400 font-semibold mb-1">By distinct species <span class="text-slate-500 font-normal">— how many different organisms</span></div>
      <div class="h-72"><canvas id="sp_taxon"></canvas></div>
      <div class="text-[11px] text-slate-400 font-semibold mt-5 mb-1">By number of runs <span class="text-slate-500 font-normal">— how much the corpus actually sampled each group (e.g. far more mammal runs than plant)</span></div>
      <div class="h-72"><canvas id="sp_taxon_runs"></canvas></div></div>`:'';

    // --- the FULL directory: every species, clickable ---
    const allsp=(s.all_species||[]);
    const allRows=allsp.map(o=>{const cm=commonName(o.organism);return [
      spName(o.organism),
      cm?`<span class="text-slate-400">${esc(cm)}</span>`:'—',
      o.taxon_group&&o.taxon_group!=='Other / unclassified'?`<span class="text-slate-400 text-xs">${esc(o.taxon_group)}</span>`:'—',
      `<span class="font-bold text-accent-400">${fmt(o.n_runs)}</span>`,
      o.n_genes!=null?`<span class="text-emerald-300">${fmt(o.n_genes)}</span>`:'—',
      o.pct_proteome&&o.pct_proteome.pct!=null?`<span class="text-teal">${o.pct_proteome.pct}%</span>`:'—',
      o.n_protein_groups!=null?`<span class="text-slate-400">${fmt(o.n_protein_groups)}</span>`:'—',
      o.organism_taxon_id?`<span class="text-slate-500 text-xs">${esc(o.organism_taxon_id)}</span>`:'—'];});
    const allCard=`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">📋 All species (${fmt(allsp.length)})</h3>
      <p class="text-[11px] text-slate-500 mb-3">Every identified organism in the corpus, sorted by run count — <b>click any row</b> to open its page. <b>Distinct genes</b> = proteome depth; <b>% of genes</b> = genes ÷ NCBI protein-coding count, <b>cumulative across all runs</b> (gene-presence, not isoform/proteoform coverage); <b>protein groups</b> = raw DIA-NN group strings (over-counts). Use the search box up top to jump to one by name (e.g. "dog", "yeast").</p>
      ${allsp.length?lbTable(['Species','Common name','Group','Runs','Distinct genes','% of genes','Protein groups','Taxon ID'],allRows,allsp.map(o=>`go('species','${(o.organism||'').replace(/'/g,"\\'")}')`)):empty('No species yet.')}</div>`;

    $('#sp_body').innerHTML=hero+sampled+deepest+rareCard+spotlight+breadth+allCard;

    if(tb.length){ const ctx=$('#sp_taxon'); if(ctx){ if(charts.sp_taxon) charts.sp_taxon.destroy(); const cc=chartColors();
      charts.sp_taxon=new Chart(ctx,{type:'bar',data:{labels:tb.map(b=>b.group),datasets:[{data:tb.map(b=>b.n_species),backgroundColor:tb.map((_,i)=>PALETTE[i%PALETTE.length]),borderRadius:6}]},
        options:{indexAxis:'y',plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>{const b=tb[c.dataIndex];return `${fmt(b.n_species)} species · ${fmt(b.n_runs)} runs`;}}}},
          scales:{x:{title:{display:true,text:'distinct species',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick,precision:0}},y:{grid:{display:false},ticks:{color:cc.tick}}},maintainAspectRatio:false}}); } }
    if(tb.length){ const ctx2=$('#sp_taxon_runs'); if(ctx2){ if(charts.sp_taxon_runs) charts.sp_taxon_runs.destroy(); const cc=chartColors();
      const colorOf={}; tb.forEach((b,i)=>{colorOf[b.group]=PALETTE[i%PALETTE.length];});
      const tbr=[...tb].sort((a,b)=>(b.n_runs||0)-(a.n_runs||0));
      charts.sp_taxon_runs=new Chart(ctx2,{type:'bar',data:{labels:tbr.map(b=>b.group),datasets:[{data:tbr.map(b=>b.n_runs),backgroundColor:tbr.map(b=>colorOf[b.group]),borderRadius:6}]},
        options:{indexAxis:'y',plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>{const b=tbr[c.dataIndex];return `${fmt(b.n_runs)} runs · ${fmt(b.n_species)} species`;}}}},
          scales:{x:{title:{display:true,text:'number of runs (searches)',color:cc.tick},grid:{color:cc.grid},ticks:{color:cc.tick,precision:0}},y:{grid:{display:false},ticks:{color:cc.tick}}},maintainAspectRatio:false}}); } }
  }catch(e){ const el=$('#sp_body'); if(el) el.innerHTML=empty('Showcase unavailable: '+esc(e.message)); }
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
    <div id="protcardbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-40 rounded-xl"></div></div>
    <div id="protsumbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-24 rounded-xl"></div></div>
    <div id="covbox" class="glass card p-5 fade-in mb-5"><div class="skeleton h-28 rounded-xl"></div></div>
    <div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">Observed peptides (${d.peptides.length})</h3>
    <p class="text-[11px] text-slate-500 mb-3">${d.peptides_sequence_mapped?'Only peptides that map onto this protein’s canonical UniProt sequence (I/L equated) — same set as the coverage map above.':(d.custom_construct?'🧪 Custom / recombinant construct (no public sequence) — these are the peptides co-observed in its runs; can’t be sequence-verified.':'⚠ Canonical sequence unavailable, so these are peptides co-observed in the same runs and may include co-eluting proteins.')}</p>
    ${table(['Peptide','Pos','Precursors','Charges','Runs','Best q','IM'],
      d.peptides.map(p=>[`<span class="font-mono text-accent-400 hover:underline">${esc(p.stripped_seq)}</span>`,p.start?`<span class="text-[11px] text-slate-500">${p.start}–${p.end}</span>`:'—',fmt(p.n_precursors),fmt(p.n_charges),fmt(p.n_runs),sci(p.best_q_value),p.has_im?'<span class="text-teal">●</span>':'—']),
      d.peptides.map(p=>`go('peptide','${encodeURIComponent(p.stripped_seq)}')`))}</div>
    <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-3">Per search / run (${d.per_search.length})</h3>
    ${table(['Search','Engine','Run','Gene','Unique pep','Precursors','Intensity','PG q'],
      d.per_search.map(r=>[`<span class="text-accent-400 cursor-pointer" onclick="event.stopPropagation();go('run','${r.search_id}')">${esc(r.search_name||'—')}</span>`,esc(r.search_engine||'—'),`<span class="font-mono text-[11px]">${esc((r.raw_path||'').split('/').pop())}</span>`,esc(r.gene||'—'),fmt(r.n_unique_peptides),fmt(r.n_precursors),sci(r.intensity),sci(r.pg_q_value)]))}</div>`;
    loadProtCard(pg); loadProtSummary(pg); loadCoverage(pg);
  }catch(e){ dbError(e); }
}

/* ---------- PROTEIN trading card (fun facts + cool stats) ---------- */
async function loadProtCard(pg){
  const el=$('#protcardbox'); if(!el)return;
  try{
    const d=await api(`/api/protein/${encodeURIComponent(pg)}/card`);
    const c=d.card||{}, w=d.wikipedia, L=d.links||{};
    if(!c.protein_group){ el.remove(); return; }
    const nOrg=c.n_organisms||0; const species=(c.species||[]);
    const headline=nOrg>1
      ? `<div class="text-3xl font-extrabold text-accent-400 kpi-num">${fmt(nOrg)} species</div>
         <div class="text-xs text-slate-400">detected across the corpus${nOrg>=5?' — broadly conserved':''}</div>`
      : (nOrg===1
        ? `<div class="text-2xl font-extrabold text-white">1 species</div><div class="text-xs text-slate-400">${esc(species[0].organism||'')}${commonName(species[0].organism)?` (${esc(commonName(species[0].organism))})`:''}</div>`
        : `<div class="text-lg font-semibold text-slate-300">Species not annotated</div>`);
    let abund='';
    if(c.abundance_percentile!=null){
      const p=c.abundance_percentile, approx=c.abundance_sampled?'~':'';
      const hue=p>=80?'#34d399':(p>=40?'#facc15':'#fb7185');
      abund=`<div class="mt-1">
        <div class="flex items-baseline justify-between"><span class="text-[11px] uppercase tracking-wider text-slate-500">Abundance rank</span>
          <span class="text-xs text-slate-300">more abundant than <b class="text-white">${approx}${fmtF(p,0)}%</b> of proteins</span></div>
        <div class="mt-1 h-2.5 rounded-full bg-white/5 overflow-hidden"><div style="width:${Math.max(2,Math.min(100,p))}%;height:100%;background:${hue}"></div></div>
        ${c.abundance_sample_n?`<div class="text-[10px] text-slate-600 mt-0.5">estimated from a random ${fmt(c.abundance_sample_n)}-measurement sample of the corpus</div>`:''}</div>`;
    }
    const chips=species.slice(0,24).map(o=>{ const cm=commonName(o.organism);
      return `<span onclick="event.stopPropagation();${o.organism&&o.organism!=='Unknown'?`go('species','${(o.organism||'').replace(/'/g,"\\'")}')`:''}"
        class="${o.organism&&o.organism!=='Unknown'?'cursor-pointer hover:bg-white/10':''} px-2 py-1 rounded-lg text-[11px] bg-white/5 text-slate-300"
        title="${esc(o.organism||'')}${cm?` (${esc(cm)})`:''} · ${fmt(o.n_runs)} runs">
        ${esc(o.organism||'—')}${cm?` <span class="text-slate-500">${esc(cm)}</span>`:''} <span class="text-slate-500">${fmt(o.n_runs)}</span></span>`;
    }).join(' ');
    const moreOrg=species.length>24?`<span class="text-[11px] text-slate-500">+${fmt(species.length-24)} more</span>`:'';
    const tr=c.top_run;
    const topRun=tr?`<div class="mt-3 text-xs text-slate-400"><span class="text-slate-500">Peak abundance:</span>
      ${tr.organism_name?`<span class="text-slate-200">${esc(tr.organism_name)}</span>${commonName(tr.organism_name)?` (${esc(commonName(tr.organism_name))})`:''} · `:''}
      <span class="font-mono text-[11px]">${esc((tr.raw_path||'').split('/').pop())}</span>
      ${tr.search_name?` · <span onclick="go('run','${esc(tr.search_id)}')" class="cursor-pointer text-accent-400 hover:underline">${esc(tr.search_name)}</span>`:''}
      ${tr.intensity!=null?` · intensity ${sci(tr.intensity)}`:''}</div>`:'';
    const mini=`<div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-3">
      ${stat('Searches',fmt(c.n_searches))}${stat('Runs',fmt(c.n_runs))}
      ${stat('Unique peptides',fmt(c.max_unique_peptides))}${stat('Best PG q',sci(c.best_pg_q))}</div>`;
    const contam=c.contaminant?`<div class="mt-3 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20">
      <div class="text-[11px] uppercase tracking-wider text-rose-300 font-semibold mb-0.5">⚠ ${esc(c.contaminant.kind||'contaminant')}</div>
      <p class="text-sm text-rose-100/90">${esc(c.contaminant.note||'')}</p></div>`:'';
    const wikiBlock=(w&&w.extract)?`<div class="mt-3 pt-3 border-t border-white/5 flex gap-3">
      ${w.image?`<img src="${esc(w.image)}" alt="" class="w-20 h-20 object-cover rounded-lg flex-shrink-0" loading="lazy" onerror="this.style.display='none'">`:''}
      <div><div class="text-[11px] text-slate-500 mb-1">📖 Did you know? · ${esc(w.title||'')}</div>
      <p class="text-sm text-slate-300">${esc(w.extract)}</p>
      ${w.url?`<a href="${esc(w.url)}" target="_blank" class="text-[11px] text-accent-400 hover:underline">Read more on Wikipedia ↗</a>`:''}</div></div>`:'';
    const links=[L.uniprot&&`<a href="${esc(L.uniprot)}" target="_blank" class="text-accent-400 hover:underline">UniProt ↗</a>`,
                 L.alphafold&&`<a href="${esc(L.alphafold)}" target="_blank" class="text-accent-400 hover:underline">AlphaFold structure ↗</a>`].filter(Boolean).join(' · ');
    el.innerHTML=`
      <div class="flex items-center justify-between flex-wrap gap-2 mb-2">
        <h3 class="font-bold text-white">Protein card <span class="text-[10px] text-slate-500 font-normal">fun facts + cool stats</span></h3>
        <div class="text-xs">${links}</div></div>
      <div class="grid md:grid-cols-2 gap-4">
        <div>${headline}${abund}${topRun}</div>
        <div>${species.length?`<div class="text-[11px] uppercase tracking-wider text-slate-500 mb-1.5">Seen in</div>
          <div class="flex flex-wrap gap-1.5 items-center">${chips} ${moreOrg}</div>`:''}</div></div>
      ${mini}${contam}${wikiBlock}`;
  }catch(e){ const el=$('#protcardbox'); if(el) el.remove(); }
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
    if(d.custom_construct){ el.innerHTML=`<h3 class="font-bold text-white mb-2">Sequence coverage</h3><div class="py-8 text-center"><div class="text-3xl mb-2">🧪</div><div class="text-slate-300 font-medium">Custom / recombinant construct</div><div class="text-sm text-slate-500 mt-1 max-w-lg mx-auto">This accession (<span class="font-mono">${esc(d.accession)}</span>) is a placeholder from a custom FASTA — an engineered or recombinant protein with no public UniProt/NCBI entry, so there's no canonical sequence to map coverage against.</div></div>`; return; }
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
async function renderSpecies(name){
  view.innerHTML=`<div class="skeleton h-64 rounded-xl"></div>`;
  const cm=commonName(name);
  const COLF=['#34d399','#22d3ee','#facc15','#f97316','#fb7185','#a78bfa','#f472b6','#4ade80','#60a5fa','#fbbf24'];
  const protLink=(p,label)=>p?`<span onclick="go('protein','${encodeURIComponent(p.protein_group)}')" class="cursor-pointer text-accent-400 hover:underline">${esc(label||p.gene||p.protein_group)}</span>`:'—';
  try{
    const d=await api(`/api/species/${encodeURIComponent(name)}`);
    const fb=d.function_breakdown||[]; const maxfb=Math.max(1,...fb.map(b=>b.n));
    view.innerHTML=`
    ${crumb([['Dashboard','dashboard'],[name,null]])}
    <div class="glass card p-6 fade-in mb-5">
      <div class="flex gap-5 flex-wrap items-start">
        <div id="spImg" class="w-40 h-40 rounded-xl bg-white/5 shrink-0 flex items-center justify-center text-3xl overflow-hidden">🔬</div>
        <div class="min-w-0 flex-1">
          <h1 class="text-2xl font-extrabold text-white italic">${esc(name)}</h1>
          ${cm?`<div class="text-slate-400">${esc(cm)}</div>`:''}
          <div id="spWiki" class="text-sm text-slate-300 mt-3 leading-relaxed">Loading fun facts…</div>
          <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
            ${stat('Distinct genes',d.n_genes!=null?fmt(d.n_genes):'—')}${d.pct_proteome?stat('Gene coverage',`<span class="text-teal">${d.pct_proteome.pct}%</span>`):''}${stat('Protein groups',fmt(d.n_protein_groups))}${stat('Runs',fmt(d.n_runs))}${stat('Searches',fmt(d.n_searches))}${stat('Contaminant flags',fmt(d.n_contaminants))}
          </div>
          ${d.pct_proteome?`<div class="mt-2 text-[11px] text-slate-500">≈ <b class="text-teal">${d.pct_proteome.pct}%</b> of protein-coding <b>genes</b> seen at least once, <b>cumulative across all runs</b> of this species (not one experiment): <b>${fmt(d.n_genes)}</b> of <b>${fmt(d.pct_proteome.ref_genes)}</b> NCBI genes${d.pct_proteome.isoform_pct!=null?` · only <b class="text-amber-300">${d.pct_proteome.isoform_pct}%</b> at isoform level (${fmt(d.pct_proteome.reviewed_isoforms)} reviewed isoforms)`:''}.</div>`:''}
        </div>
      </div>
    </div>
    ${d.coolest_protein?`<div class="glass card p-5 fade-in mb-5 border border-plum/30">
      <h3 class="font-bold text-white mb-1">✨ Coolest-named protein</h3>
      <div class="text-lg text-plum font-semibold">${esc(d.coolest_protein.name||d.coolest_protein.gene)}</div>
      <div class="text-sm text-slate-400">${esc(d.coolest_protein.why)}${d.coolest_protein.name?` · gene <b>${esc(d.coolest_protein.gene)}</b>`:''} · ${protLink(d.coolest_protein,'view protein →')}</div>
    </div>`:''}
    <div class="grid md:grid-cols-2 gap-4 mb-5">
      <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-1">💪 Most abundant protein</h3>
        ${d.most_abundant?`<div class="text-base text-emerald-300 font-semibold">${esc(d.most_abundant.gene||d.most_abundant.protein_group)}</div>
        <div class="text-xs text-slate-400">seen in ${fmt(d.most_abundant.n_runs)} runs · ${protLink(d.most_abundant,'view →')}</div>`:empty('—')}</div>
      <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-1">🔍 Least abundant (still detected)</h3>
        ${d.least_abundant?`<div class="text-base text-amber-300 font-semibold">${esc(d.least_abundant.gene||d.least_abundant.protein_group)}</div>
        <div class="text-xs text-slate-400">${fmt(d.least_abundant.n_runs)} runs · ${protLink(d.least_abundant,'view →')}</div>`:empty('—')}</div>
    </div>
    ${fb.length?`<div class="glass card p-5 fade-in mb-5"><h3 class="font-bold text-white mb-1">What kinds of proteins did we find?</h3>
      <p class="text-[11px] text-slate-500 mb-3">Identified proteins grouped by function (heuristic, by gene family) — blood, liver, muscle, immune, …</p>
      <div class="space-y-1.5">${fb.map((b,i)=>`<div class="flex items-center gap-2 text-xs">
        <span class="w-32 shrink-0 text-slate-300">${esc(b.label)}</span>
        <div class="flex-1 h-4 rounded bg-white/5 overflow-hidden"><div style="width:${Math.round(100*b.n/maxfb)}%;background:${COLF[i%COLF.length]}" class="h-full"></div></div>
        <span class="w-10 text-right tabular-nums text-slate-400">${fmt(b.n)}</span></div>`).join('')}</div></div>`:''}
    <div class="glass card p-5 fade-in"><h3 class="font-bold text-white mb-1">Most-seen proteins</h3>
      <p class="text-[11px] text-slate-500 mb-3">Detected in the most runs for this species — click through to the protein page.</p>
      ${table(['Gene','Protein group','Runs','Searches','Precursors','Peptides'],
        (d.top_seen||[]).map(p=>[esc(p.gene||'—'),`<span class="font-mono text-accent-400 hover:underline">${esc(p.protein_group)}</span>`,fmt(p.n_runs),fmt(p.n_searches),fmt(p.sum_prec),fmt(p.max_pep)]),
        (d.top_seen||[]).map(p=>`go('protein','${encodeURIComponent(p.protein_group)}')`))}</div>`;
    loadSpeciesWiki(name, cm);
  }catch(e){
    if(e.status===404){ view.innerHTML=`${crumb([['Dashboard','dashboard'],[name,null]])}`
      +`<div class="glass card p-8 text-center fade-in"><div class="text-4xl mb-2">🔬</div>
        <h2 class="text-lg font-bold text-white mb-1">${esc(name)}</h2>
        <p class="text-slate-400 text-sm">No proteins recorded for this organism in the corpus yet. If it was just ingested, the species view catches up on the next refresh.</p></div>`; }
    else { dbError(e); }
  }
}

async function loadSpeciesWiki(name, cm){
  for(const t of [cm, name].filter(Boolean)){   // common name first (better hit for animals), then scientific
    try{ const d=await api(`/api/wiki?title=${encodeURIComponent(t)}`); const w=d.wiki;
      if(w && w.extract){
        const wb=$('#spWiki'); if(wb) wb.innerHTML=`${esc(w.extract)} ${w.url?`<a href="${w.url}" target="_blank" class="text-accent-400 hover:underline">Wikipedia ↗</a>`:''}`;
        if(w.image){ const im=$('#spImg'); if(im) im.innerHTML=`<img src="${esc(w.image)}" alt="${esc(name)}" class="w-full h-full object-cover">`; }
        return;
      }
    }catch(e){}
  }
  const wb=$('#spWiki'); if(wb) wb.innerHTML='<span class="text-slate-500">No encyclopedia entry found for this organism.</span>';
}

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

/* Download an export for a search (kind='report' → DIA-NN report.parquet for DE-LIMP/LIMPA;
   kind='brief' → markdown research brief for a HIVE Claude). Fetches first so we can surface a clean
   error for searches without quant, then triggers a client-side download. */
async function exportReport(id, btn, kind){
  kind = kind || 'report';
  const cfg = {report:{url:'/api/export/diann_report/', done:fn=>'Exported '+fn+' — upload it into DE-LIMP to run LIMPA'},
               brief:{url:'/api/export/research_brief/', done:fn=>'Exported '+fn+' — hand it to a HIVE Claude (proteomics-pipeline skill)'},
               resubmit:{url:'/api/export/resubmit_brief/', done:fn=>'Exported '+fn+' — hand it to a HIVE/Flinders Claude to re-search this un-ingested data with DIA-NN'}}[kind];
  const label = btn ? btn.textContent : '';
  if(btn){ btn.textContent='⏳ Building…'; btn.style.pointerEvents='none'; }
  try{
    const res = await fetch(cfg.url+encodeURIComponent(id));
    if(!res.ok){ let msg='HTTP '+res.status; try{ const j=await res.json(); msg=j.detail||msg; }catch(e){} throw new Error(msg); }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition')||''; const m = cd.match(/filename="([^"]+)"/);
    const fn = (m&&m[1]) || (kind==='brief'?'research_brief.md':'report.parquet');
    const url = URL.createObjectURL(blob); const a=document.createElement('a');
    a.href=url; a.download=fn; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    toast(cfg.done(fn));
  }catch(e){ toast('Export failed: '+((e&&e.message)||'error')); }
  finally{ if(btn){ btn.textContent=label; btn.style.pointerEvents=''; } }
}

/* ---------- LAB PORTAL: a logged-in PI/submitter's OWN data (tier = lab) ---------- */
async function renderMyData(){
  view.innerHTML=`<section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">📁 My Submissions <span class="text-[11px] font-bold text-emerald-300 align-middle">YOUR LAB</span></h1>
    <p class="text-slate-400 text-sm mt-1">Your CoreOmics submissions to the UC Davis Proteomics Core and the FRAN searches behind them — matched to your UC Davis login. Only your own data is shown.</p></section>
    <div class="glass card p-4 fade-in" id="myBody"><div class="skeleton h-64 rounded-xl"></div></div>`;
  try{
    const d=await api('/api/my'); const subs=d.submissions||[], searches=d.searches||[];
    if(!subs.length && !searches.length){ $('#myBody').innerHTML=empty('No submissions are linked to your account yet. If you expect data here, contact the Proteomics Core.'); return; }
    const subTbl = subs.length ? table(
      ['Submission','PI','Submitter','Institute','Submitted','Samples','Type','Status'],
      subs.map(s=>[`<span class="font-mono text-accent-400">${esc(s.submission_id)}</span>`,
        esc([s.pi_first_name,s.pi_last_name].filter(Boolean).join(' ')||'—'),
        esc([s.submitter_first_name,s.submitter_last_name].filter(Boolean).join(' ')||'—'),
        esc(s.institute||'—'), s.submitted_at?esc(String(s.submitted_at)):'—',
        s.num_samples!=null?fmt(s.num_samples):'—', esc(s.proteomics_type||'—'), esc(s.status||'—')]))
      : '<div class="text-xs text-slate-500">No CoreOmics submissions found.</div>';
    const srchTbl = searches.length ? table(
      ['Search','Project','Submission','Engine','Precursors','Proteins','DE-LIMP'],
      searches.map(r=>[esc(r.real_search_name||'—'), esc(r.project||'—'),
        r.coreomics_submission_id?`<span class="font-mono text-slate-400">${esc(r.coreomics_submission_id)}</span>`:'—',
        esc(r.search_engine||'—'), r.n_precursors_total!=null?fmt(r.n_precursors_total):'—',
        r.n_proteins_total!=null?fmt(r.n_proteins_total):'—',
        `<button onclick="exportReport('${esc(r.search_id)}',this)" class="px-2 py-0.5 rounded text-[11px] font-semibold bg-accent/20 text-accent-400 hover:bg-accent/30" title="Download a report.parquet to run in DE-LIMP (LIMPA)">⬇ Export</button>`]))
      : '<div class="text-xs text-slate-500">No FRAN searches are linked to your submissions yet.</div>';
    $('#myBody').innerHTML=`<div class="text-xs text-slate-500 mb-2">${fmt(subs.length)} submission${subs.length===1?'':'s'} · ${fmt(searches.length)} search${searches.length===1?'':'es'} linked to your account</div>`
      +`<h3 class="font-bold text-white mb-2">Submissions</h3>${subTbl}`
      +`<h3 class="font-bold text-white mt-5 mb-2">Searches</h3>${srchTbl}`;
  }catch(e){ dbError(e,'#myBody'); }
}

/* ---------- INTERNAL: collaborator browser (private deployment only) ---------- */
async function renderCollaborators(){
  view.innerHTML=`<section class="mb-5 fade-in"><h1 class="text-2xl font-extrabold text-white tracking-tight">🔒 Collaborators <span class="text-[11px] font-bold text-rose-300 align-middle">CONFIDENTIAL</span></h1>
    <p class="text-slate-400 text-sm mt-1">Private core-facility directory — real client / PI / project from search provenance. Click a collaborator to see all their searches with real names + file locations.</p></section>
    <div class="glass card p-4 fade-in" id="collabBody"><div class="skeleton h-64 rounded-xl"></div></div>`;
  try{
    const d=await api('/api/internal/collaborators'); const rows=d.collaborators||[];
    if(!rows.length){ $('#collabBody').innerHTML=empty('No provenance rows.'); return; }
    const coBadge=c=>c==='confirmed'?'<span class="text-emerald-300" title="confirmed via an existing CoreOmics link">●</span>':c==='suggested'?'<span class="text-amber-300" title="suggested by name match — advisory, unconfirmed">○</span>':'';
    const coCell=r=>r.co_pi?`<span class="text-slate-300">${esc(r.co_pi)}</span> ${coBadge(r.co_confidence)}${r.co_institute?`<div class="text-[10px] text-slate-500">${esc(r.co_institute)}</div>`:''}`:'<span class="text-slate-600">—</span>';
    const foot=`<div class="text-[11px] text-slate-500 mt-3">${fmt(d.n_internal_standard||0)} internal/standard group${(d.n_internal_standard===1)?'':'s'} hidden · ${fmt(d.n_unattributed_searches||0)} searches unattributed (no service folder — standards / QC / staging)</div>`;
    $('#collabBody').innerHTML=`<div class="text-xs text-slate-500 mb-3">${fmt(rows.length)} collaborators — by curated service-directory folder · CoreOmics PI is advisory (● confirmed · ○ suggested)</div>`+table(
      ['Collaborator','Searches','PIs','LIMS-linked','CoreOmics PI · institute','Campus'],
      rows.map(r=>[`<span class="font-semibold text-accent-400">${esc(r.client)}</span>`,fmt(r.n_searches),fmt(r.n_pis),r.n_lims_linked?fmt(r.n_lims_linked):'<span class="text-slate-600">—</span>',coCell(r),r.campus?esc(r.campus):'—']),
      rows.map(r=>`go('collab','${(r.client||'').replace(/'/g,"\\'")}')`))
      +foot+`<div id="labsByInst" class="mt-6"><div class="skeleton h-32 rounded-xl"></div></div>`;
    loadLabsByInstitution();
  }catch(e){ dbError(e,'#collabBody'); }
}

async function loadLabsByInstitution(){
  const el=$('#labsByInst'); if(!el) return;
  try{
    const d=await api('/api/internal/labs'); const insts=d.institutions||[];
    if(!insts.length){ el.innerHTML=''; return; }
    el.innerHTML=`<div class="flex items-baseline gap-2 mb-2"><h3 class="font-bold text-white">Labs by institution</h3>
      <span class="text-[11px] text-slate-500">${fmt(d.n_labs)} PI labs across ${fmt(d.n_institutions)} institutions — every CoreOmics lab we have data for. <span class="text-emerald-300">green</span> = analyzed in FRAN, <span class="text-amber-300">📦 amber</span> = data on the share, not yet ingested. Click a lab.</span></div>
      <div class="grid md:grid-cols-2 gap-3">${insts.map(i=>`
        <div class="glass card p-3">
          <div class="flex items-baseline justify-between gap-2 mb-2">
            <span class="font-semibold text-white text-sm">${esc(i.institute)}</span>
            <span class="text-[10px] text-slate-500">${fmt(i.n_labs)} lab${i.n_labs===1?'':'s'}${i.n_uningested?` · <span class="text-amber-300/80">${fmt(i.n_uningested)} 📦 un-ingested</span>`:''} · ${fmt(i.n_searches)} searches</span></div>
          <div class="flex flex-wrap gap-1.5">${i.labs.map(l=>{
            const un=l.status!=='analyzed';
            const cls=un?'bg-amber-500/10 text-amber-300/80 hover:ring-amber-400/40':'bg-emerald-500/10 text-emerald-300 hover:ring-emerald-400/40';
            const cnt=un?`📦 ${fmt(l.n_submissions)}`:fmt(l.n_searches);
            const tip=`${un?'NOT yet ingested — data on the share. ':''}${[l.college,l.department].filter(Boolean).map(esc).join(' · ')}${(l.college||l.department)?' — ':''}${fmt(l.n_submissions)} CoreOmics submissions · ${fmt(l.n_searches)} FRAN searches`;
            return `<span onclick="go('lab','${esc((l.pi||'').replace(/'/g,"\\'"))}')" class="cursor-pointer px-2 py-1 rounded-lg text-xs ${cls} hover:ring-1" title="${tip}">${esc(l.pi)} <span class="opacity-60">${cnt}</span>${l.department?`<span class="opacity-40"> · ${esc(l.department)}</span>`:''}</span>`;
          }).join('')}</div>
        </div>`).join('')}</div>`;
  }catch(e){ el.innerHTML=`<div class="text-xs text-slate-500">Labs-by-institution unavailable: ${esc((e&&e.message)||'error')}</div>`; }
}
async function renderCollaborator(name){
  view.innerHTML=`${crumb([['Collaborators','collaborators'],[name,null]])}<div class="glass card p-4 fade-in" id="collabBody"><div class="skeleton h-64 rounded-xl"></div></div>`;
  try{
    const d=await api(`/api/internal/collaborator/${encodeURIComponent(name)}`); const rows=d.searches||[];
    if(!rows.length){ $('#collabBody').innerHTML=empty('No searches for this collaborator.'); return; }
    const nLinked=rows.filter(r=>r.coreomics_submission_id).length;
    const conf=s=>({'auto:name+date':'<span class="text-emerald-300" title="name + date matched">●●●</span>','auto:name+year':'<span class="text-amber-300" title="name + year matched">●●</span>','auto:name':'<span class="text-rose-300" title="name only — review">●</span>','manual':'<span class="text-teal" title="manually confirmed">✓</span>'}[s]||'');
    $('#collabBody').innerHTML=`<h2 class="text-xl font-bold text-white mb-1">${esc(name)}</h2><div class="text-xs text-slate-500 mb-3">${fmt(rows.length)} searches · ${fmt(nLinked)} linked to CoreOmics · real names + file paths (confidential)</div>`+table(
      ['Search','Organism','CoreOmics PI · institute','Submission','Conf','Engine','Precursors','Proteins','Report path'],
      rows.map(r=>{
        const coPI=[r.pi_first_name,r.pi_last_name].filter(Boolean).join(' ')||[r.submitter_first_name,r.submitter_last_name].filter(Boolean).join(' ');
        // person-resolved (no submission, but we matched the PI from the folder hint): customer_contact = "Name · Institute"
        const ccName=(r.customer_contact||'').split(' · ')[0], ccInst=(r.customer_contact||'').split(' · ')[1];
        const coCell=r.coreomics_submission_id
          ? `${coPI?`<span class="text-emerald-300 cursor-pointer hover:underline" onclick="event.stopPropagation();go('lab','${esc((coPI||'').replace(/'/g,"\\'"))}')" title="Open lab page — all submissions + searches">${esc(coPI)}</span>`:'<span class="text-emerald-300">(linked)</span>'}${r.co_institute?`<div class="text-[10px] text-slate-500">${esc(r.co_institute)}</div>`:''}${r.coreomics_submission_id?`<div class="text-[10px]"><span class="text-slate-500 cursor-pointer hover:underline" onclick="event.stopPropagation();go('submission','${esc(r.coreomics_submission_id)}')">#${esc(r.coreomics_submission_id)}</span></div>`:''}`
          : (ccName?`<span class="text-teal cursor-pointer hover:underline" onclick="event.stopPropagation();go('lab','${esc(ccName.replace(/'/g,"\\'"))}')" title="PI resolved from folder name (not a confirmed CoreOmics submission)">${esc(ccName)}</span>${ccInst?`<div class="text-[10px] text-slate-500">${esc(ccInst)}</div>`:''}<div class="text-[9px] text-slate-600">PI from folder</div>`
             : '<span class="text-slate-600">—</span>');
        const sub=r.coreomics_submission_id
          ? `<span class="text-slate-300">${r.co_submitted||'—'}</span>${r.co_num_samples!=null?`<div class="text-[10px] text-slate-500">${fmt(r.co_num_samples)} samples</div>`:''}`
          : '—';
        return [
          `<span class="text-accent-400 cursor-pointer hover:underline" onclick="event.stopPropagation();go('run','${esc(r.search_id)}')" title="Open this search's results in the corpus">${esc(r.real_search_name||'—')}</span>${r.pi?`<div class="text-[10px] text-slate-500">path: ${esc(r.pi)}</div>`:''}`,
          r.organism?`<span class="italic text-accent-400">${esc(r.organism)}</span>`:'—',
          coCell, sub, conf(r.linkage_status)||'<span class="text-slate-600">—</span>',
          r.search_engine?esc(r.search_engine):'—', r.n_precursors_total!=null?fmt(r.n_precursors_total):'—',
          r.n_proteins_total!=null?fmt(r.n_proteins_total):'—',
          `<span class="font-mono text-[10px] text-slate-400 break-all">${esc(r.report_path||'—')}</span>`];
      }));
  }catch(e){ dbError(e,'#collabBody'); }
}

async function renderSubmission(id){
  view.innerHTML=`${crumb([['Search','dashboard'],['Submission #'+id,null]])}<div class="glass card p-4 fade-in" id="subBody"><div class="skeleton h-64 rounded-xl"></div></div>`;
  try{
    const d=await api(`/api/internal/submission/${encodeURIComponent(id)}`);
    const sub=d.submission, rows=d.searches||[], samples=d.samples||[];
    const pi=sub?([sub.pi_first_name,sub.pi_last_name].filter(Boolean).join(' ')||'—'):'—';
    const submitter=sub?[sub.submitter_first_name,sub.submitter_last_name].filter(Boolean).join(' '):'';
    const head = sub
      ? `<h2 class="text-xl font-bold text-white mb-1">Submission #${esc(id)}</h2>
         <div class="grid grid-cols-2 md:grid-cols-4 gap-3 my-3">
           ${stat('CoreOmics PI', pi!=='—'?`<span class="cursor-pointer hover:underline" onclick="go('lab','${esc((pi||'').replace(/'/g,"\\'"))}')" title="Open lab page">${esc(pi)}</span>`:'—')}
           ${submitter?stat('Submitter', esc(submitter)):''}
           ${sub.institute?stat('Institute', `<span class="text-base">${esc(sub.institute)}</span>`):''}
           ${sub.submitted_at?stat('Submitted', esc(sub.submitted_at)):''}
           ${sub.num_samples!=null?stat('Samples', fmt(sub.num_samples)):''}
           ${sub.proteomics_type?stat('Type', `<span class="text-base">${esc(sub.proteomics_type)}</span>`):''}
           ${sub.organism?stat('Organism', `<span class="text-base italic text-accent-400">${esc(sub.organism)}</span>`):''}
         </div>`
      : `<h2 class="text-xl font-bold text-white mb-1">Submission #${esc(id)}</h2>
         <div class="text-xs text-amber-300 mb-3">This submission isn't in the CoreOmics cache — showing the FRAN searches linked to its ID.</div>`;
    const searchTable = rows.length
      ? `<div class="text-xs text-slate-500 mb-2 mt-4">${fmt(rows.length)} FRAN search${rows.length===1?'':'es'} linked to this submission</div>`+table(
          ['Search','Engine','Organism','Precursors','Proteins'],
          rows.map(r=>[
            `<span class="text-accent-400 cursor-pointer hover:underline" onclick="event.stopPropagation();go('run','${esc(r.search_id)}')">${esc(r.real_search_name||'—')}</span>${r.client?`<div class="text-[10px] text-slate-500">${esc(r.client)}</div>`:''}`,
            r.search_engine?esc(r.search_engine):'—',
            r.organism?`<span class="italic text-accent-400">${esc(r.organism)}</span>`:'—',
            r.n_precursors_total!=null?fmt(r.n_precursors_total):'—',
            r.n_proteins_total!=null?fmt(r.n_proteins_total):'—']))
      : empty('No FRAN searches are linked to this submission yet.');
    const sampleChips = samples.length
      ? `<div class="text-xs text-slate-500 mb-2 mt-5">${fmt(samples.length)} CoreOmics samples</div><div class="flex flex-wrap gap-2">${samples.slice(0,200).map(s=>`<span class="px-2.5 py-1 rounded-lg text-xs glass text-slate-300" title="${esc(s.condition_name||'')}">${esc(s.sample_name||s.unique_id||'—')}${s.condition_name?` <span class="opacity-60">· ${esc(s.condition_name)}</span>`:''}</span>`).join('')}</div>`
      : '';
    // raw-data location on the service directory (Windows / Linux-Flinders / macOS), from the disk-match
    const sd=d.service_dir;
    const serviceDirCard = sd ? `<div class="glass card p-3 mb-3">
        <div class="flex items-center gap-2 mb-2 flex-wrap"><span class="text-[11px] uppercase tracking-wider text-slate-500">Raw data — service directory</span>
          ${sd.in_fran?'<span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300">✅ analyzed in FRAN</span>':'<span class="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300">📦 on the share · not yet ingested</span>'}
          ${sd.run_count?`<span class="text-[10px] text-slate-500">${fmt(sd.run_count)} runs</span>`:''}</div>
        <div class="text-xs font-mono text-slate-300 space-y-0.5 break-all">
          ${sd.service_folder_win?`<div><span class="text-slate-500 inline-block w-[72px]">Windows</span> ${esc(sd.service_folder_win)}</div>`:''}
          ${sd.service_folder?`<div><span class="text-slate-500 inline-block w-[72px]">Linux/HIVE</span> /nfs/lssc0/flinders/proteomics/Data/lab/service/${esc(sd.service_folder)}</div>
          <div><span class="text-slate-500 inline-block w-[72px]">macOS</span> /Volumes/proteomics/Data/lab/service/${esc(sd.service_folder)}</div>`:''}
        </div>
        ${!sd.in_fran?`<button onclick="exportReport('${esc(id)}',this,'resubmit')" class="mt-2 px-2 py-1 rounded text-[11px] font-semibold bg-plum/20 text-plum hover:bg-plum/30" title="Download a re-search brief (file locations + submission info) for a HIVE/Flinders Claude to search this un-ingested data with DIA-NN">🔄 Re-search this data</button>`:''}
      </div>` : '';
    $('#subBody').innerHTML=head+serviceDirCard+searchTable+sampleChips;
  }catch(e){ dbError(e,'#subBody'); }
}

async function renderLab(pi){
  view.innerHTML=`${crumb([['Collaborators','collaborators'],[pi,null]])}<div class="glass card p-4 fade-in" id="labBody"><div class="skeleton h-64 rounded-xl"></div></div>`;
  try{
    const d=await api(`/api/internal/lab/${encodeURIComponent(pi)}`);
    const subs=d.submissions||[], rows=d.searches||[];
    const head=`<h2 class="text-xl font-bold text-white mb-1">${esc(d.pi||pi)}<span class="text-sm font-normal text-slate-500"> — lab</span></h2>
      <div class="text-xs text-slate-500 mb-3">${(d.institutes&&d.institutes.length)?esc(d.institutes.join(' · ')):'institute unknown'} · ${fmt(d.n_submissions||subs.length)} CoreOmics submissions · ${fmt(d.n_searches||rows.length)} FRAN searches (confidential)</div>
      <div class="flex flex-wrap gap-3 mb-3 text-xs">
        <span class="px-2.5 py-1 rounded-lg bg-emerald-500/10 text-emerald-300">✅ ${fmt(d.n_analyzed||0)} analyzed in FRAN</span>
        <span class="px-2.5 py-1 rounded-lg bg-amber-500/10 text-amber-300" title="Data is on the share but not yet ingested into FRAN">📦 ${fmt(d.n_on_share||0)} on share · un-ingested</span>
      </div>`;
    // ---- PI profile banner (grants / lab website / photo) — shown once the enrichment fills it ----
    const p=d.profile;
    const grants=(p&&Array.isArray(p.grants_json))?p.grants_json:[];
    const profileCard = p ? `<div class="glass card p-4 mb-4 flex gap-4 items-start fade-in">
        ${p.photo_url?`<img src="${esc(p.photo_url)}" alt="" class="w-20 h-20 rounded-xl object-cover flex-shrink-0 border border-white/10" onerror="this.style.display='none'">`:''}
        <div class="min-w-0 flex-1">
          ${p.lab_url?`<a href="${esc(p.lab_url)}" target="_blank" rel="noopener" class="text-accent-400 hover:underline text-sm font-semibold">${esc(p.lab_url.replace('https://','').replace('http://',''))} ↗</a>`:''}
          ${p.research_blurb?`<p class="text-slate-300 text-sm mt-1 leading-snug">${esc(p.research_blurb)}</p>`:''}
          ${grants.length?`<div class="mt-2"><div class="text-[11px] uppercase tracking-wider text-slate-500 mb-1">Active grants (${fmt(grants.length)})</div>${grants.slice(0,5).map(g=>`<div class="text-xs text-slate-400 mb-0.5">• ${esc(g.title||g.project_title||'')}${g.agency||g.amount?` <span class="opacity-60">(${[g.agency,g.amount].filter(Boolean).map(esc).join(' · ')})</span>`:''}</div>`).join('')}</div>`:''}
        </div></div>` : '';
    // ---- lab search stats ----
    const st=d.stats||{};
    const kpi=(l,v)=>`<div><span class="text-slate-500">${l}</span> <span class="text-white font-semibold">${v}</span></div>`;
    const statsCard = rows.length ? `<div class="glass card p-4 mb-4">
        <div class="flex flex-wrap gap-x-5 gap-y-1 text-xs mb-3">
          ${kpi('Searches', fmt(d.n_searches||rows.length))}
          ${kpi('Proteins (Σ)', fmt(st.total_proteins||0))}
          ${kpi('Precursors (Σ)', fmt(st.total_precursors||0))}
          ${st.first_submission?kpi('Active', `${esc(st.first_submission)} → ${esc(st.last_submission)}`):''}
        </div>
        ${(st.organisms&&st.organisms.length)?`<div class="text-[11px] text-slate-500 mb-1">Organisms studied</div><div class="flex flex-wrap gap-1.5 mb-3">${st.organisms.map(o=>`<span class="px-2 py-0.5 rounded text-xs bg-white/5 text-accent-400 italic">${esc(o)}</span>`).join('')}</div>`:''}
        ${(st.top_proteins&&st.top_proteins.length)?`<div class="text-[11px] text-slate-500 mb-1">Most-identified proteins (across this lab's searches)</div><div class="flex flex-wrap gap-1.5">${st.top_proteins.map(tp=>`<span class="px-2 py-0.5 rounded text-xs bg-emerald-500/10 text-emerald-300" title="found in ${fmt(tp.n_searches)} of their searches">${esc(tp.protein)} <span class="opacity-50">${fmt(tp.n_searches)}</span></span>`).join('')}</div>`:''}
        ${(st.engines&&st.engines.length)?`<div class="text-[10px] text-slate-600 mt-2">Engines: ${st.engines.map(esc).join(', ')}</div>`:''}
      </div>` : '';
    // per-submission data-location status (analyzed / on-share-uningested / not located)
    const dataCell=s=> s.data_status==='analyzed'
        ? '<span class="text-emerald-300" title="analyzed in FRAN">✅ in FRAN</span>'
        : s.data_status==='on_share'
        ? `<span class="text-amber-300">📦 on share${s.run_count?` · ${fmt(s.run_count)} runs`:''}</span>${s.service_folder?`<div class="text-[10px] text-slate-500 font-mono break-all mt-0.5" title="${esc(s.service_folder_win||'')}">${esc(s.service_folder)}</div>`:''}<button onclick="event.stopPropagation();exportReport('${esc(s.submission_id)}',this,'resubmit')" class="mt-1 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-plum/20 text-plum hover:bg-plum/30" title="Download a HIVE/Flinders re-search brief (file locations + full submission info) to re-search this un-ingested data with DIA-NN or your Claude proteomics skill">🔄 Re-search this data</button>`
        : '<span class="text-slate-600" title="no matching folder found on the share">❔ not located</span>';
    const aboutCell=s=>{
      const txt=((s.description||s.mass_spec_wanted||'')+'').trim();
      const tags=[s.dia?'DIA':'', s.tmt?'TMT':'', s.prot_or_pep].filter(Boolean).map(t=>`<span class="text-[9px] px-1 rounded bg-white/5 text-slate-400">${esc(t)}</span>`).join(' ');
      const main=txt?`<span class="text-slate-400" title="${esc(txt)}">${esc(txt.slice(0,64))}${txt.length>64?'…':''}</span>`:(s.sample_prep?`<span class="text-[10px] text-slate-500">${esc(s.sample_prep)}</span>`:'<span class="text-slate-600">—</span>');
      return `${main}${tags?`<div class="mt-0.5">${tags}</div>`:''}`;
    };
    const subTable=subs.length
      ? `<div class="text-xs text-slate-500 mb-2 mt-4">Submissions <span class="text-slate-600">— ✅ analyzed · 📦 on share (ready to ingest) · ❔ not located</span></div>`+table(
          ['Submission','Submitted','Samples','Type','Organism','About','Data'],
          subs.map(s=>[
            `<span onclick="event.stopPropagation();go('submission','${esc(s.submission_id)}')" class="text-accent-400 cursor-pointer hover:underline font-mono text-xs">#${esc(s.submission_id)}</span>`,
            s.submitted_at?esc(s.submitted_at):'—', s.num_samples!=null?fmt(s.num_samples):'—',
            s.proteomics_type?esc(s.proteomics_type):'—',
            s.organism?`<span class="italic text-accent-400">${esc(s.organism)}</span>`:'—',
            aboutCell(s), dataCell(s)]))
      : empty('No CoreOmics submissions found for this surname.');
    const searchTable=rows.length
      ? `<div class="text-xs text-slate-500 mb-2 mt-5">Search results</div>`+table(
          ['Search','Engine','Organism','Submission','Precursors'],
          rows.map(r=>[
            `<span class="text-accent-400 cursor-pointer hover:underline" onclick="event.stopPropagation();go('run','${esc(r.search_id)}')">${esc(r.real_search_name||'—')}</span>${r.client?`<div class="text-[10px] text-slate-500">${esc(r.client)}</div>`:''}`,
            r.search_engine?esc(r.search_engine):'—',
            r.organism?`<span class="italic text-accent-400">${esc(r.organism)}</span>`:'—',
            r.coreomics_submission_id?`<span onclick="event.stopPropagation();go('submission','${esc(r.coreomics_submission_id)}')" class="text-accent-400 cursor-pointer hover:underline font-mono text-xs">#${esc(r.coreomics_submission_id)}</span>`:'<span class="text-slate-600">—</span>',
            r.n_precursors_total!=null?fmt(r.n_precursors_total):'—']))
      : empty('No searches linked to this lab yet.');
    $('#labBody').innerHTML=head+profileCard+statsCard+subTable+searchTable;
  }catch(e){ dbError(e,'#labBody'); }
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
function dbError(e, sel){
  // A statement-timeout (the corpus is being ingested and a live read briefly stalled) is NOT an
  // outage — show a calm "busy, retry" rather than the alarming "Database unavailable" (which is
  // only right for a missing credential / real connection failure).
  const msg=(e&&e.message)||''; const busy = e && e.status===503 && /cancel|timeout|terminat|too many connections|server closed|connection/i.test(msg);
  const html = busy
    ? `<div class="glass card p-8 text-center fade-in"><div class="w-12 h-12 mx-auto rounded-full bg-teal/15 grid place-items-center mb-3"><svg width="24" height="24" fill="none" stroke="#00B5E2" stroke-width="2" viewBox="0 0 24 24" class="spin"><path d="M21 12a9 9 0 1 1-6.2-8.5"/></svg></div>
        <h3 class="font-bold text-white">Just a moment — the corpus is busy</h3>
        <p class="text-sm text-slate-400 mt-1 max-w-md mx-auto">New data is being ingested right now, so this view timed out. It usually clears within a few seconds.</p>
        <button onclick="location.reload()" class="mt-4 px-4 py-2 rounded-lg tab-active text-sm font-semibold">Retry</button></div>`
    : `<div class="glass card p-8 text-center fade-in"><div class="w-12 h-12 mx-auto rounded-full bg-amber-500/15 grid place-items-center mb-3"><svg width="24" height="24" fill="none" stroke="#fbbf24" stroke-width="2" viewBox="0 0 24 24"><path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg></div>
        <h3 class="font-bold text-white">Database unavailable</h3><p class="text-sm text-slate-400 mt-1 max-w-md mx-auto">${esc(e.message)}</p>
        <p class="text-xs text-slate-500 mt-3">The browser is wired to the live PG Farm <code>delimp</code> DB. Set the credential (HF Secret <code>DELIMP_PG_PASSWORD</code> or <code>DELIMP_PG_TOKEN_FILE</code>) and reload.</p></div>`;
  if(sel) $(sel).innerHTML=html; else view.innerHTML=html; }

/* nav buttons */
document.querySelectorAll('.navbtn').forEach(b=>b.addEventListener('click',()=>go(b.dataset.view)));

/* internal-view badge (real filenames) when a core-facility key is in use */
if(INTKEY){ const n=document.getElementById('navtabs'); if(n) n.insertAdjacentHTML('beforeend','<span class="ml-2 px-2 py-0.5 rounded text-[10px] bg-accent/20 text-accent-400 font-semibold" title="Core-facility view: real filenames shown">internal</span>'); }

/* AUTH UI — TIER-AWARE, and fetched LIVE from /api/me so a cached page can never show a stale
   login state. Tiers: public (sanitized) · lab (own data → "My Submissions") · full (core staff →
   everything, the Collaborators directory + CONFIDENTIAL reveal). */
function applyAuth(st){
  const tier=(st&&st.tier)||'public', isFull=!!(st&&st.is_full), who=((st&&st.name)||'').trim();
  window.__FRAN_INTERNAL__ = isFull;          // legacy flag == the full (core-staff) confidential view
  window.__FRAN_TIER__ = tier;
  const tog=(id,hide)=>{const e=document.getElementById(id); if(e) e.classList.toggle('hidden', hide);};
  tog('nav_collab', !isFull); tog('nav_collab_m', !isFull);     // Collaborators dir = full only
  tog('nav_mydata', tier!=='lab'); tog('nav_mydata_m', tier!=='lab'); // My Submissions = lab users
  // tier badge in the navbar
  let badge=document.getElementById('tierBadge');
  if(!badge){ const n=document.getElementById('navtabs'); if(n){ n.insertAdjacentHTML('beforeend','<span id="tierBadge" class="hidden"></span>'); badge=document.getElementById('tierBadge'); } }
  if(badge){
    if(isFull){ badge.className='ml-2 px-2 py-0.5 rounded text-[10px] bg-rose-500/20 text-rose-300 font-bold tracking-wide'; badge.title='Core-facility view — real names & paths. Do not share.'; badge.textContent='🔒 CONFIDENTIAL'; }
    else if(tier==='lab'){ badge.className='ml-2 px-2 py-0.5 rounded text-[10px] bg-emerald-500/20 text-emerald-300 font-bold tracking-wide'; badge.title='Viewing your own lab’s submissions only.'; badge.textContent='🔓 YOUR LAB'; }
    else { badge.className='hidden'; badge.textContent=''; }
  }
  const box=document.getElementById('authBox');
  if(box){
    if(tier!=='public'){
      box.innerHTML=`<span class="text-[11px] text-slate-400 mr-2 hidden lg:inline" title="Logged in via UC Davis SSO">${who?('🔓 '+esc(who)):'🔓 signed in'}</span>`
        +`<a href="/logout" class="px-2.5 py-1.5 rounded-lg hover:bg-white/5 text-slate-300 text-xs font-medium" title="Sign out">Log out</a>`;
    } else {
      const back=encodeURIComponent(location.pathname+location.search);
      box.innerHTML=`<a href="/login?post=${back}" class="px-2.5 py-1.5 rounded-lg hover:bg-white/5 text-slate-300 text-xs font-medium" title="UC Davis staff & PIs: log in for confidential / your-lab data">Log in</a>`;
    }
  }
}
// paint immediately from the server-baked hint, then CORRECT from the live endpoint (cache-proof)
applyAuth({tier: window.__FRAN_TIER__, is_full: window.__FRAN_INTERNAL__, name: window.__FRAN_USER__});
api('/api/me').then(applyAuth).catch(()=>{});

/* boot */
// version is injected server-side + known client-side — show it immediately, independent of /health
// (so a DB hiccup never blanks the version the way it used to).
(function(){ const v=window.__APP_VERSION__; const vb=$('#appVersion'); if(vb && v && v.indexOf('__')<0) vb.textContent='v'+v; })();
route(); pollHealth(); refreshFooterCounts();
setInterval(pollHealth, 15000);
setInterval(refreshFooterCounts, 30000); // watch it populate
