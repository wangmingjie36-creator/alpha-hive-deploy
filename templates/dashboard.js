// ── Safe localStorage (F1: 隐私模式/配额满时不崩溃) ──
function _ls(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v);}catch(e){return null;}}
// ── Unified Namespace (F4: 减少全局污染) ──
window.AH=window.AH||{};
// ── Global Error Boundary ──
(function(){
  var errCount=0, maxToast=3;
  function showErrToast(msg){
    if(errCount>=maxToast)return;
    errCount++;
    var t=document.createElement('div');
    t.className='ah-err-toast';
    t.textContent='\u26a0\ufe0f '+msg;
    t.style.cssText='position:fixed;bottom:'+(20+errCount*50)+'px;right:20px;'
      +'background:#ff4444;color:#fff;padding:10px 16px;border-radius:8px;'
      +'font-size:13px;z-index:99999;opacity:0.95;max-width:350px;'
      +'box-shadow:0 2px 8px rgba(0,0,0,.3);transition:opacity .3s';
    document.body.appendChild(t);
    setTimeout(function(){t.style.opacity='0';setTimeout(function(){t.remove();errCount--;},400)},6000);
  }
  window.onerror=function(msg,src,line){
    console.error('[AH]',msg,src,line);
    showErrToast((msg||'Unknown error').toString().slice(0,80));
  };
  window.onunhandledrejection=function(e){
    var r=e.reason||{};
    var msg=(r.message||r.toString()||'Promise rejected').slice(0,80);
    console.error('[AH] Unhandled rejection:',r);
    showErrToast(msg);
  };
})();

// ── F13: Service Worker Registration ──
if('serviceWorker' in navigator && location.protocol==='https:'){
  navigator.serviceWorker.register('sw.js').catch(function(){});
}

// ── Auto dark mode from system preference ──
(function(){
  var mq = window.matchMedia('(prefers-color-scheme: dark)');
  if(mq.matches && !_ls('ah-theme')) {
    document.documentElement.classList.add('dark');
  }
  mq.addEventListener('change', function(e) {
    if(!_ls('ah-theme')) {
      document.documentElement.classList.toggle('dark', e.matches);
    }
  });
})();

// ── Scroll-to-top ──
(function(){
  const btn=document.getElementById('scrollTop');
  if(!btn)return;
  window.addEventListener('scroll',function(){
    if(window.scrollY>400)btn.classList.add('show');
    else btn.classList.remove('show');
  },{passive:true});
})();

// ── Dark Mode ──
var _darkTimer;
function toggleDark(){
  const h=document.documentElement;
  h.classList.toggle('dark');
  const isDark=h.classList.contains('dark');
  _ls('ahDark',isDark?'1':'0');
  _ls('ah-theme',isDark?'dark':'light');
  document.getElementById('darkBtn').textContent=isDark?'☀️ 亮色':'🌙 暗黑';
  chartInstances.forEach(function(c){try{c.destroy();}catch(e){}});
  chartInstances.length=0;
  if(window.AH.rendered){
    Object.keys(window.AH.rendered).forEach(function(k){delete window.AH.rendered[k];});
  }
  document.querySelectorAll('canvas.rendered').forEach(function(c){
    c.classList.remove('rendered');
    const w=c.closest('.chart-canvas-wrap')||c.closest('.radar-wrap');
    if(w) w.classList.remove('skel-done');
  });
  clearTimeout(_darkTimer);
  _darkTimer=setTimeout(function(){
    if(window.AH.renderChart){
      ['fgChart','scoresChart','dirChart'].forEach(window.AH.renderChart);
    }
    if(window.AH.renderRadar && window.AH.radarKeys){
      window.AH.radarKeys.forEach(window.AH.renderRadar);
    }
    if(window.AH.initFgTrend) window.AH.initFgTrend();
    if(window.AH.trendChart){
      try{
        const tc=isDark?'rgba(255,255,255,.65)':'rgba(0,0,0,.55)';
        const gc=isDark?'rgba(255,255,255,.07)':'rgba(0,0,0,.06)';
        const s=window.AH.trendChart.options.scales;
        if(s.x){s.x.grid.color=gc;s.x.ticks.color=tc;}
        if(s.y){s.y.grid.color=gc;s.y.ticks.color=tc;}
        if(s.y1&&s.y1.ticks)s.y1.ticks.color='#F4A532';
        const leg=window.AH.trendChart.options.plugins.legend;
        if(leg&&leg.labels)leg.labels.color=tc;
        window.AH.trendChart.update();
      }catch(e){}
    }
  },50);
}
if(_ls('ahDark')==='1'){
  document.documentElement.classList.add('dark');
}
document.addEventListener('DOMContentLoaded',function(){
  const b=document.getElementById('darkBtn');
  if(b&&document.documentElement.classList.contains('dark'))b.textContent='☀️ 亮色';
});

// ── Hamburger Menu ──
function toggleMenu(){
  const nav=document.getElementById('navLinks');
  const ov=document.getElementById('navOverlay');
  if(!nav||!ov)return;
  nav.classList.toggle('open');
  ov.classList.toggle('open');
}
document.querySelectorAll('.nav-link').forEach(function(l){
  l.addEventListener('click',function(){
    const nav=document.getElementById('navLinks');
    const ov=document.getElementById('navOverlay');
    if(nav)nav.classList.remove('open');
    if(ov)ov.classList.remove('open');
  });
});

// ── Share Functions ──
function showToast(msg){
  const t=document.getElementById('toast');
  if(!t)return;
  t.textContent=msg;
  t.classList.add('show');
  setTimeout(function(){t.classList.remove('show');},2200);
}
function shareToX(){
  const txt=encodeURIComponent('【Alpha Hive 日报】去中心化蜂群智能投资研究，今日扫描完成！\\n\\n');
  const url=encodeURIComponent(window.location.href);
  window.open('https://twitter.com/intent/tweet?text='+txt+'&url='+url,'_blank','width=550,height=420');
}
function copyLink(){
  if(navigator.clipboard){
    navigator.clipboard.writeText(window.location.href).then(function(){
      showToast('链接已复制到剪贴板');
    });
  }else{
    showToast('浏览器不支持剪贴板');
  }
}
function shareCard(ticker,score){
  const txt=encodeURIComponent('Alpha Hive 蜂群信号：$'+ticker+' 综合分 '+score.toFixed(1)+'/10\\n\\n');
  const url=encodeURIComponent(window.location.href);
  window.open('https://twitter.com/intent/tweet?text='+txt+'&url='+url,'_blank','width=550,height=420');
}

// ── Export CSV ──
function exportCSV(){
  const tbl=document.getElementById('oppTable');
  if(!tbl)return;
  const rows=tbl.querySelectorAll('tr');
  let csv='\\uFEFF';
  rows.forEach(function(tr){
    if(tr.style.display==='none')return;
    const cols=tr.querySelectorAll('th,td');
    const line=[];
    cols.forEach(function(c){line.push('"'+c.textContent.trim().replace(/"/g,'""')+'"');});
    csv+=line.join(',')+'\\n';
  });
  const blob=new Blob([csv],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='alpha-hive-opportunities.csv';
  a.click();
  URL.revokeObjectURL(a.href);
  showToast('CSV 已导出');
}

// ── Filter ──
function applyFilter(f,btn){
  document.querySelectorAll('.filter-btn').forEach(function(b){b.classList.remove('active');});
  if(btn)btn.classList.add('active');
  const items=document.querySelectorAll('.scard[data-dir]');
  const trs=document.querySelectorAll('#oppTable tbody tr[data-dir]');
  const cards=document.querySelectorAll('.company-card[data-dir]');
  let count=0;
  function check(el,doCount){
    const d=el.getAttribute('data-dir');
    const s=parseFloat(el.getAttribute('data-score'));
    let show=false;
    if(f==='all')show=true;
    else if(f==='high')show=s>=7.5;
    else show=d===f;
    el.style.display=show?'':'none';
    if(show&&doCount)count++;
  }
  items.forEach(function(el){check(el,false);});
  trs.forEach(function(el){check(el,true);});
  cards.forEach(function(el){check(el,false);});
  const fc=document.getElementById('filterCount');
  if(fc)fc.textContent=f==='all'?'':'显示 '+count+' 条结果';
}

// ── Table scroll hint ──
(function(){
  const w=document.querySelector('.tbl-wrap');
  if(!w)return;
  function check(){
    if(w.scrollWidth>w.clientWidth+2)w.classList.add('has-scroll');
    else w.classList.remove('has-scroll');
  }
  check();
  window.addEventListener('resize',check,{passive:true});
  w.addEventListener('scroll',function(){
    if(w.scrollLeft+w.clientWidth>=w.scrollWidth-4)w.classList.remove('has-scroll');
    else if(w.scrollWidth>w.clientWidth+2)w.classList.add('has-scroll');
  },{passive:true});
})();

// ── Table Search ──
function filterTable(){
  const q=document.getElementById('tableSearch').value.toLowerCase();
  const rows=document.querySelectorAll('#oppTable tbody tr');
  let shown=0;
  rows.forEach(function(tr){
    const vis=tr.textContent.toLowerCase().includes(q);
    tr.style.display=vis?'':'none';
    if(vis)shown++;
  });
  const st=document.getElementById('filterStatus');
  if(st)st.textContent=q?(shown?'显示 '+shown+' 条结果':'未找到匹配的标的'):'';
}

// ── Table Sort ──
document.querySelectorAll('#oppTable thead th').forEach(function(th,i){
  th.addEventListener('click',function(){
    const tbody=document.querySelector('#oppTable tbody');
    const rows=Array.from(tbody.rows).filter(function(r){return r.style.display!=='none';});
    const asc=th.getAttribute('data-sort')!=='asc';
    document.querySelectorAll('#oppTable thead th').forEach(function(t){t.removeAttribute('data-sort');t.setAttribute('aria-sort','none');});
    th.setAttribute('data-sort',asc?'asc':'desc');
    th.setAttribute('aria-sort',asc?'ascending':'descending');
    rows.sort(function(a,b){
      const av=a.cells[i].textContent.trim();
      const bv=b.cells[i].textContent.trim();
      const an=parseFloat(av),bn=parseFloat(bv);
      if(!isNaN(an)&&!isNaN(bn)) return asc?an-bn:bn-an;
      return asc?av.localeCompare(bv,'zh'):bv.localeCompare(av,'zh');
    });
    rows.forEach(function(r){tbody.appendChild(r);});
  });
});

// ── Charts (lazy via IntersectionObserver) ──
let chartInstances=[];
(function(){
  const rendered={};

  function markDone(id){
    const c=document.getElementById(id);
    if(c){
      c.classList.add('rendered');
      const w=c.closest('.chart-canvas-wrap')||c.closest('.radar-wrap');
      if(w) w.classList.add('skel-done');
    }
  }

  function renderChart(id){
    if(rendered[id])return;
    if(typeof Chart==='undefined')return;
    rendered[id]=true;
    const dark=document.documentElement.classList.contains('dark');
    const tc=dark?'rgba(255,255,255,.65)':'rgba(0,0,0,.55)';
    const gc=dark?'rgba(255,255,255,.07)':'rgba(0,0,0,.06)';
    try{

    if(id==='fgChart'){
      const fgCtx=document.getElementById('fgChart');
      if(!fgCtx)return;
      const fv=__AH__.fv;
      const fc=fv<=25?'#ef4444':fv<=45?'#f97316':fv<=55?'#f59e0b':fv<=75?'#22c55e':'#16a34a';
      const fl=__AH__.fg_label;
      chartInstances.push(new Chart(fgCtx,{
        type:'doughnut',
        data:{datasets:[{data:[fv,100-fv],backgroundColor:[fc,dark?'#2a3050':'#e8ecf3'],
                           borderWidth:0,circumference:180,rotation:-90}]},
        options:{responsive:true,maintainAspectRatio:false,cutout:'72%',
                 plugins:{legend:{display:false},tooltip:{enabled:false}}},
        plugins:[{id:'fgTxt',afterDraw:function(ch){
          const cx=ch.ctx,w=ch.width,h=ch.height;
          cx.save();
          cx.font='bold 26px system-ui';cx.fillStyle=fc;cx.textAlign='center';cx.textBaseline='middle';
          cx.fillText(fv,w/2,h*.60);
          cx.font='11px system-ui';cx.fillStyle=tc;cx.fillText(fl,w/2,h*.60+20);
          cx.restore();
        }}]
      }));
      markDone('fgChart');
    }

    if(id==='scoresChart'){
      const scCtx=document.getElementById('scoresChart');
      if(!scCtx)return;
      const sc=__AH__.scores;
      const clrs=sc.map(function(x){return x[1]>=7?'rgba(34,197,94,.85)':x[1]>=5.5?'rgba(245,158,11,.85)':'rgba(239,68,68,.85)';});
      chartInstances.push(new Chart(scCtx,{
        type:'bar',
        data:{labels:sc.map(function(x){return x[0];}),
               datasets:[{data:sc.map(function(x){return x[1];}),backgroundColor:clrs,borderRadius:5,borderSkipped:false}]},
        options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,
                 onClick:function(evt,elems){
                   if(!elems.length)return;
                   const idx=elems[0].index;
                   const tk=sc[idx][0];
                   scrollToDeep(tk);
                 },
                 plugins:{legend:{display:false},tooltip:{callbacks:{label:function(c){return' '+c.raw+'/10';}}}},
                 scales:{
                   x:{min:0,max:10,grid:{color:gc},ticks:{color:tc,font:{size:10}}},
                   y:{grid:{display:false},ticks:{color:tc,font:{size:10,weight:'bold'}}}
                 }}
      }));
      markDone('scoresChart');
    }

    if(id==='dirChart'){
      const dirCtx=document.getElementById('dirChart');
      if(!dirCtx)return;
      const dd=__AH__.dir_counts;
      chartInstances.push(new Chart(dirCtx,{
        type:'doughnut',
        data:{labels:['看多','看空','中性'],
               datasets:[{data:dd,
                           backgroundColor:['rgba(34,197,94,.85)','rgba(239,68,68,.85)','rgba(245,158,11,.85)'],
                           borderColor:'transparent',borderWidth:0}]},
        options:{responsive:true,maintainAspectRatio:false,cutout:'58%',
                 plugins:{legend:{position:'bottom',labels:{color:tc,font:{size:10},boxWidth:11,padding:10}},
                           tooltip:{callbacks:{label:function(c){return' '+c.label+': '+c.raw+' 只';}}}}}
      }));
      markDone('dirChart');
    }

    }catch(e){console.warn('Chart render error ('+id+'):',e);}
  }

  // Radar per ticker (lazy)
  const rd=__AH__.radar;
  const rl=['信号强度','催化剂','情绪','赔率','风险控制'];
  function renderRadar(tk){
    if(rendered['radar-'+tk])return;
    if(typeof Chart==='undefined')return;
    const dark=document.documentElement.classList.contains('dark');
    const tc=dark?'rgba(255,255,255,.65)':'rgba(0,0,0,.55)';
    const gc=dark?'rgba(255,255,255,.07)':'rgba(0,0,0,.06)';
    rendered['radar-'+tk]=true;
    const cv=document.getElementById('radar-'+tk);
    if(!cv)return;
    try{
    chartInstances.push(new Chart(cv,{
      type:'radar',
      data:{labels:rl,datasets:[{data:rd[tk],fill:true,
               backgroundColor:'rgba(102,126,234,.13)',borderColor:'#667eea',
               pointBackgroundColor:'#667eea',pointBorderColor:'#fff',pointRadius:2,borderWidth:1.5}]},
      options:{responsive:true,maintainAspectRatio:true,
               onClick:function(evt,elems){
                 if(!elems.length)return;
                 const dimIdx=elems[0].index;
                 const card=document.getElementById('deep-'+tk);
                 if(!card)return;
                 const metrics=card.querySelectorAll('.cc-metric');
                 metrics.forEach(function(m,i){
                   m.style.background=i===dimIdx?'rgba(244,165,50,.15)':'';
                 });
                 card.scrollIntoView({behavior:'smooth',block:'center'});
               },
               scales:{r:{min:0,max:100,beginAtZero:true,
                            grid:{color:gc},angleLines:{color:gc},
                            ticks:{display:false},
                            pointLabels:{color:tc,font:{size:8}}}},
               plugins:{legend:{display:false}}}
    }));
    markDone('radar-'+tk);
    }catch(e){console.warn('Radar render error ('+tk+'):',e);}
  }

  if(!('IntersectionObserver' in window)){
    // fallback: render all immediately
    ['fgChart','scoresChart','dirChart'].forEach(renderChart);
    Object.keys(rd).forEach(renderRadar);
    return;
  }

  const cobs=new IntersectionObserver(function(entries,observer){
    entries.forEach(function(en){
      if(!en.isIntersecting)return;
      const el=en.target;
      const id=el.id||el.getAttribute('data-chart-id');
      let ok=false;
      if(id&&id.indexOf('radar-')===0){
        renderRadar(id.replace('radar-',''));
        ok=rendered['radar-'+id.replace('radar-','')];
      }else if(id){
        renderChart(id);
        ok=rendered[id];
      }
      if(ok) observer.unobserve(el);
    });
  },{rootMargin:'200px 0px'});

  // Observe chart canvases
  ['fgChart','scoresChart','dirChart'].forEach(function(cid){
    const el=document.getElementById(cid);
    if(el)cobs.observe(el);
  });
  Object.keys(rd).forEach(function(tk){
    const el=document.getElementById('radar-'+tk);
    if(el)cobs.observe(el);
  });

  // Fallback: when Chart.js CDN finishes loading, render any charts still pending
  window.addEventListener('load',function(){
    if(typeof Chart==='undefined')return;
    ['fgChart','scoresChart','dirChart'].forEach(renderChart);
    Object.keys(rd).forEach(renderRadar);
  });
  // Expose for toggleDark re-render
  window.AH.rendered=rendered;
  window.AH.renderChart=renderChart;
  window.AH.renderRadar=renderRadar;
  window.AH.radarKeys=Object.keys(rd);
})();

// ── Accuracy Direction Chart ──
(function(){
  const ctx = document.getElementById('accDirChart');
  if (!ctx) return;
  const dirs  = __AH__.acc_dir_labels;
  const accs  = __AH__.acc_dir_accs;
  const tots  = __AH__.acc_dir_tots;
  chartInstances.push(new Chart(ctx, {
    type: 'bar',
    data: {
      labels: dirs,
      datasets: [{
        label: '准确率 %',
        data: accs,
        backgroundColor: ['#22c55e','#ef4444','#94a3b8'],
        borderRadius: 6,
        maxBarThickness: 40,
      }]
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: {
        callbacks: { label: function(c){ return c.raw.toFixed(1)+'% ('+tots[c.dataIndex]+' 次)'; } }
      } },
      scales: {
        x: { min:0, max:100, ticks:{ callback: function(v){ return v+'%'; } } },
        y: { grid: { display: false } }
      }
    }
  }));
})();

// ── Accuracy Ticker Table Sort ──
(function(){
  const tbl = document.getElementById('accTickerTable');
  if (!tbl) return;
  tbl.querySelectorAll('thead th').forEach(function(th, i){
    th.addEventListener('click', function(){
      const tbody = tbl.querySelector('tbody');
      const rows  = Array.from(tbody.rows);
      const asc   = th.getAttribute('data-sort') !== 'asc';
      tbl.querySelectorAll('thead th').forEach(function(t){ t.removeAttribute('data-sort'); });
      th.setAttribute('data-sort', asc ? 'asc' : 'desc');
      rows.sort(function(a, b){
        const av = a.cells[i].textContent.trim();
        const bv = b.cells[i].textContent.trim();
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? av.localeCompare(bv, 'zh') : bv.localeCompare(av, 'zh');
      });
      rows.forEach(function(r){ tbody.appendChild(r); });
    });
  });
})();

// ── F11: Win Rate Trend Chart ──
(function(){
  const cv=document.getElementById('accWinTrendChart');
  if(!cv||typeof Chart==='undefined')return;
  const wd=__AH__.acc_weekly;
  if(!wd||!wd.length)return;
  const dark=document.documentElement.classList.contains('dark');
  const tc=dark?'rgba(255,255,255,.65)':'rgba(0,0,0,.55)';
  const gc=dark?'rgba(255,255,255,.07)':'rgba(0,0,0,.06)';
  chartInstances.push(new Chart(cv,{
    type:'line',
    data:{
      labels:wd.map(function(d){return d.week;}),
      datasets:[
        {label:'胜率%',data:wd.map(function(d){return d.accuracy;}),
          borderColor:'#667eea',backgroundColor:'rgba(102,126,234,.1)',fill:true,
          tension:.3,pointRadius:3,borderWidth:2,yAxisID:'y'},
        {label:'均收益%',data:wd.map(function(d){return d.avg_ret;}),
          borderColor:'#F4A532',backgroundColor:'transparent',
          borderDash:[4,3],tension:.3,pointRadius:2,borderWidth:1.5,yAxisID:'y1'}
      ]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{position:'bottom',labels:{color:tc,font:{size:9},boxWidth:10,padding:6}}},
      scales:{
        x:{grid:{display:false},ticks:{color:tc,font:{size:8},maxRotation:45}},
        y:{position:'left',min:0,max:100,grid:{color:gc},ticks:{color:tc,font:{size:9},callback:function(v){return v+'%';}}},
        y1:{position:'right',grid:{display:false},ticks:{color:'#F4A532',font:{size:9},callback:function(v){return v+'%';}}}
      }
    }
  }));
})();
window.addEventListener('pagehide',function(){chartInstances.forEach(function(c){try{c.destroy()}catch(e){}});chartInstances=[];});
/* F37: Pause SVG SMIL animations when prefers-reduced-motion */
(function(){const mq=window.matchMedia('(prefers-reduced-motion:reduce)');function toggle(e){const svgs=document.querySelectorAll('svg');svgs.forEach(function(s){try{if(e.matches)s.pauseAnimations();else s.unpauseAnimations();}catch(ex){}});}if(mq.matches)document.addEventListener('DOMContentLoaded',function(){toggle(mq);});mq.addEventListener('change',toggle);})();

// ── F6: Scroll to deep card ──
function scrollToDeep(ticker){
  const el=document.getElementById('deep-'+ticker);
  if(!el)return;
  el.scrollIntoView({behavior:'smooth',block:'center'});
  el.classList.add('highlight');
  setTimeout(function(){el.classList.remove('highlight');},1000);
}

// ── F7b: F&G Trend Mini Chart ──
const _fgTrendHist=__AH__.fg_history;
window.AH.initFgTrend=function(){
  if(!_fgTrendHist||_fgTrendHist.length<2)return;
  const cv=document.getElementById('fgTrendChart');
  if(!cv||typeof Chart==='undefined')return;
  chartInstances.push(new Chart(cv,{
    type:'line',
    data:{
      labels:_fgTrendHist.map(function(d){return d.date.slice(5);}),
      datasets:[{
        data:_fgTrendHist.map(function(d){return d.value;}),
        borderColor:'#F4A532',backgroundColor:'rgba(244,165,50,.1)',
        fill:true,tension:.3,pointRadius:2,borderWidth:1.5
      }]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:function(c){return'F&G: '+c.raw;}}}},
      scales:{
        x:{display:true,ticks:{font:{size:8},maxRotation:0},grid:{display:false}},
        y:{display:false,min:0,max:100}
      }
    }
  }));
};
window.AH.initFgTrend();

// ── F8a: Trend Chart ──
(function(){
  const trendData=__AH__.trend_data;
  const cv=document.getElementById('trendChart');
  if(!cv||typeof Chart==='undefined')return;
  const dark=document.documentElement.classList.contains('dark');
  const tc=dark?'rgba(255,255,255,.65)':'rgba(0,0,0,.55)';
  const gc=dark?'rgba(255,255,255,.07)':'rgba(0,0,0,.06)';
  const colors=['#667eea','#F4A532','#22c55e','#ef4444','#764ba2','#f59e0b','#06b6d4','#ec4899','#8b5cf6','#14b8a6'];
  const tickers=Object.keys(trendData);
  // 收集所有日期
  const allDates={};
  tickers.forEach(function(tk){
    trendData[tk].forEach(function(d){allDates[d.date]=true;});
  });
  const dates=Object.keys(allDates).sort();
  // 默认显示前 5 个 ticker
  const activeTickers={};
  tickers.slice(0,5).forEach(function(tk){activeTickers[tk]=true;});
  // 生成 chips
  const chipWrap=document.getElementById('trendChips');
  if(chipWrap){
    tickers.forEach(function(tk,i){
      const chip=document.createElement('button');
      chip.className='trend-chip'+(activeTickers[tk]?' active':'');
      chip.textContent=tk;
      chip.onclick=function(){
        activeTickers[tk]=!activeTickers[tk];
        chip.classList.toggle('active');
        updateTrendChart();
      };
      chipWrap.appendChild(chip);
    });
  }
  const trendChart=window.AH.trendChart=new Chart(cv,{
    type:'line',
    data:{labels:dates.map(function(d){return d.slice(5);}),datasets:[]},
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:true,position:'bottom',labels:{color:tc,font:{size:10},boxWidth:12,padding:8}}},
      scales:{
        x:{grid:{color:gc},ticks:{color:tc,font:{size:10}}},
        y:{min:0,max:10,grid:{color:gc},ticks:{color:tc,font:{size:10}}}
      },
      interaction:{mode:'index',intersect:false}
    }
  });
  chartInstances.push(trendChart);
  function updateTrendChart(){
    const datasets=[];
    tickers.forEach(function(tk,i){
      if(!activeTickers[tk])return;
      const scoreMap={};
      trendData[tk].forEach(function(d){scoreMap[d.date]=d.score;});
      datasets.push({
        label:tk,
        data:dates.map(function(d){return d in scoreMap?scoreMap[d]:null;}),
        borderColor:colors[i%colors.length],
        backgroundColor:colors[i%colors.length]+'22',
        tension:.3,pointRadius:3,borderWidth:2,
        spanGaps:true
      });
    });
    trendChart.data.datasets=datasets;
    trendChart.update();
  }
  updateTrendChart();
  window.AH.updateTrendChart=updateTrendChart;
})();

// ── F8b: Diff ──
const _histFull=__AH__.hist_full;
(function(){
  const dates=Object.keys(_histFull).sort().reverse();
  const selA=document.getElementById('diffDateA');
  const selB=document.getElementById('diffDateB');
  if(!selA||!selB||dates.length<1)return;
  dates.forEach(function(d,i){
    const oA=document.createElement('option');oA.value=d;oA.textContent=d;
    selA.appendChild(oA);
    const oB=document.createElement('option');oB.value=d;oB.textContent=d;
    selB.appendChild(oB);
  });
  if(dates.length>=2)selB.selectedIndex=1;
})();

function showDiff(){
  const selA=document.getElementById('diffDateA');
  const selB=document.getElementById('diffDateB');
  const res=document.getElementById('diffResult');
  if(!selA||!selB||!res)return;
  const dA=selA.value,dB=selB.value;
  const opsA=_histFull[dA]||[];
  const opsB=_histFull[dB]||[];
  const mapA={};opsA.forEach(function(o){mapA[o.ticker]=o;});
  const mapB={};opsB.forEach(function(o){mapB[o.ticker]=o;});
  const allTk={};opsA.forEach(function(o){allTk[o.ticker]=true;});opsB.forEach(function(o){allTk[o.ticker]=true;});
  const tickers=Object.keys(allTk).sort();
  const dirCn={bullish:'看多',bearish:'看空',neutral:'中性'};
  let html='<table class="diff-table"><thead><tr><th>标的</th><th>'+dA+'</th><th>'+dB+'</th><th>变化</th><th>状态</th></tr></thead><tbody>';
  tickers.forEach(function(tk){
    const a=mapA[tk],b=mapB[tk];
    let cls='',status='';
    if(a&&!b){cls='diff-new';status='🆕 新增';}
    else if(!a&&b){cls='diff-removed';status='❌ 移除';}
    else{status='—';}
    const sA=a?a.score.toFixed(1):'-';
    const sB=b?b.score.toFixed(1):'-';
    let change='';
    if(a&&b){
      const diff=a.score-b.score;
      if(Math.abs(diff)>=0.1){
        change='<span class="'+(diff>0?'diff-up':'diff-down')+'">'+(diff>0?'↑':'↓')+Math.abs(diff).toFixed(1)+'</span>';
      }else{change='—';}
      const dirA=(dirCn[a.direction]||a.direction);
      const dirB=(dirCn[b.direction]||b.direction);
      if(dirA!==dirB)status='🔄 '+dirB+'→'+dirA;
    }
    html+='<tr class="'+cls+'"><td><strong>'+tk+'</strong></td><td>'+sA+'</td><td>'+sB+'</td><td>'+change+'</td><td>'+status+'</td></tr>';
  });
  html+='</tbody></table>';
  res.innerHTML=html;
}

// ── F9: Keyboard shortcuts ──
(function(){
  let cards=[];
  let activeIdx=-1;
  document.addEventListener('DOMContentLoaded',function(){
    cards=Array.from(document.querySelectorAll('.scard[data-dir]'));
  });
  document.addEventListener('keydown',function(e){
    const tag=document.activeElement.tagName;
    if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT')return;
    if(e.key==='j'||e.key==='J'){
      e.preventDefault();
      activeIdx=Math.min(activeIdx+1,cards.length-1);
      focusCard(activeIdx);
    }
    if(e.key==='k'||e.key==='K'){
      e.preventDefault();
      activeIdx=Math.max(activeIdx-1,0);
      focusCard(activeIdx);
    }
    if(e.key==='d'&&!e.ctrlKey&&!e.metaKey){
      toggleDark();
    }
    if(e.key==='?'){
      toggleKbHelp();
    }
    if(e.key==='Escape'){
      const h=document.getElementById('kbHelp');
      if(h)h.style.display='none';
    }
  });
  function focusCard(idx){
    cards.forEach(function(c){c.style.outline='';});
    if(idx>=0&&idx<cards.length){
      cards[idx].style.outline='2px solid var(--acc)';
      cards[idx].scrollIntoView({behavior:'smooth',block:'center'});
    }
  }
})();
function toggleKbHelp(){
  const h=document.getElementById('kbHelp');
  if(!h)return;
  h.style.display=h.style.display==='flex'?'none':'flex';
}

// ── Global Search (F12) ──
(function(){
  const si=__AH__.search_index;
  const inp=document.getElementById('globalSearch');
  const box=document.getElementById('gsResults');
  if(!inp||!box)return;
  let selIdx=-1;

  inp.addEventListener('input',function(){
    const q=inp.value.trim().toUpperCase();
    selIdx=-1;
    if(!q){box.innerHTML='';box.style.display='none';return;}
    const hits=si.filter(function(x){return x.ticker.toUpperCase().indexOf(q)>=0;});
    if(!hits.length){
      box.innerHTML='<div class="gs-empty">未找到匹配的标的</div>';
      box.style.display='block';return;
    }
    let html='';
    hits.forEach(function(h,i){
      const dc=h.direction==='看多'?'var(--bull)':h.direction==='看空'?'var(--bear)':'var(--ts)';
      const p=h.price?'$'+h.price:'';
      html+='<div class="gs-item" data-idx="'+i+'" data-ticker="'+h.ticker+'">'
        +'<span style="font-weight:700">'+h.ticker+'</span>'
        +'<span style="color:'+dc+';font-size:.82em">'+h.direction+'</span>'
        +'<span style="font-size:.82em;color:var(--ts)">'+h.score+'/10</span>'
        +'<span style="font-size:.82em;color:var(--ts)">'+p+'</span>'
        +'</div>';
    });
    box.innerHTML=html;
    box.style.display='block';
    box.querySelectorAll('.gs-item').forEach(function(el){
      el.addEventListener('click',function(){
        pickResult(el.getAttribute('data-ticker'));
      });
    });
  });

  inp.addEventListener('keydown',function(e){
    const items=box.querySelectorAll('.gs-item');
    if(!items.length)return;
    if(e.key==='ArrowDown'){
      e.preventDefault();
      selIdx=Math.min(selIdx+1,items.length-1);
      hlItem(items);
    }else if(e.key==='ArrowUp'){
      e.preventDefault();
      selIdx=Math.max(selIdx-1,0);
      hlItem(items);
    }else if(e.key==='Enter'){
      e.preventDefault();
      if(selIdx>=0&&items[selIdx]){
        pickResult(items[selIdx].getAttribute('data-ticker'));
      }else if(items.length===1){
        pickResult(items[0].getAttribute('data-ticker'));
      }
    }else if(e.key==='Escape'){
      box.innerHTML='';box.style.display='none';
      inp.blur();
    }
  });

  function hlItem(items){
    items.forEach(function(el,i){
      el.style.background=i===selIdx?'var(--surface2)':'';
    });
    if(selIdx>=0&&items[selIdx])items[selIdx].scrollIntoView({block:'nearest'});
  }

  function pickResult(ticker){
    box.innerHTML='';box.style.display='none';
    inp.value=ticker;
    scrollToDeep(ticker);
    // 同时高亮表格行
    const rows=document.querySelectorAll('#oppTable tbody tr');
    rows.forEach(function(r){
      const tickerCell=r.cells[1];
      if(tickerCell&&tickerCell.textContent.trim()===ticker){
        r.style.background='rgba(244,165,50,.12)';
        setTimeout(function(){r.style.background='';},2000);
      }
    });
  }

  // Cmd+K / Ctrl+K 快捷键聚焦搜索
  document.addEventListener('keydown',function(e){
    if((e.metaKey||e.ctrlKey)&&e.key==='k'){
      e.preventDefault();
      inp.focus();
      inp.select();
    }
  });

  // 点击外部关闭
  document.addEventListener('click',function(e){
    if(!inp.contains(e.target)&&!box.contains(e.target)){
      box.innerHTML='';box.style.display='none';
    }
  });
})();

// ── F14: Hash Router ──
(function(){
  const sections=['today','charts','list','deep','report','trend','history','accuracy'];
  const sectionEls={};
  sections.forEach(function(s){ sectionEls[s]=document.getElementById(s); });

  const _orig=window.scrollToDeep;

  function navigateTo(route){
    const m=route.match(/^\/stock\/(.+)$/);
    if(m){ _orig(m[1]); return; }
    const sec=route.replace(/^\//,'');
    const el=sectionEls[sec];
    if(el){ el.scrollIntoView({behavior:'smooth',block:'start'}); hlNav(sec); }
  }

  function hlNav(sec){
    document.querySelectorAll('.nav-link').forEach(function(l){
      l.classList.toggle('active', l.getAttribute('href')==='#/'+sec);
    });
  }

  function norm(h){
    if(!h||h==='#')return '';
    if(h.charAt(1)!=='/'){const n='#/'+h.slice(1); history.replaceState(null,'',n); return n.slice(1); }
    return h.slice(1);
  }

  window.addEventListener('hashchange',function(){ const r=norm(location.hash); if(r)navigateTo(r); });

  const init=norm(location.hash);
  if(init) setTimeout(function(){ navigateTo(init); },100);

  window.scrollToDeep=function(tk){ _orig(tk); history.pushState(null,'','#/stock/'+tk); };

  if('IntersectionObserver' in window){
    const obs=new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if(en.isIntersecting && sections.indexOf(en.target.id)>=0){
          history.replaceState(null,'','#/'+en.target.id);
          hlNav(en.target.id);
        }
      });
    },{rootMargin:'-40% 0px -55% 0px'});
    sections.forEach(function(s){ if(sectionEls[s]) obs.observe(sectionEls[s]); });
  }
})();

// ── Event Listeners (replacing inline onclick handlers) ──
(function(){
  // Dark mode toggle
  var darkBtn=document.getElementById('darkBtn');
  if(darkBtn) darkBtn.addEventListener('click',function(){ toggleDark(); });

  // Hamburger menu
  var hamburgerBtn=document.getElementById('hamburgerBtn');
  if(hamburgerBtn) hamburgerBtn.addEventListener('click',function(){ toggleMenu(); });

  // Nav overlay close
  var navOverlay=document.getElementById('navOverlay');
  if(navOverlay) navOverlay.addEventListener('click',function(){ toggleMenu(); });

  // Share bar buttons
  var btnShareX=document.getElementById('btn-share-x');
  if(btnShareX) btnShareX.addEventListener('click',function(){ shareToX(); });

  var btnCopyLink=document.getElementById('btn-copy-link');
  if(btnCopyLink) btnCopyLink.addEventListener('click',function(){ copyLink(); });

  var btnPrint=document.getElementById('btn-print');
  if(btnPrint) btnPrint.addEventListener('click',function(){ window.print(); });

  var btnExportCSV=document.getElementById('btn-export-csv');
  if(btnExportCSV) btnExportCSV.addEventListener('click',function(){ exportCSV(); });

  // Filter buttons
  var filterMap={
    'filter-all':'all',
    'filter-bullish':'bullish',
    'filter-bearish':'bearish',
    'filter-neutral':'neutral',
    'filter-high':'high'
  };
  Object.keys(filterMap).forEach(function(id){
    var btn=document.getElementById(id);
    if(btn) btn.addEventListener('click',function(){ applyFilter(filterMap[id],btn); });
  });

  // Diff button
  var btnDiff=document.getElementById('btn-diff');
  if(btnDiff) btnDiff.addEventListener('click',function(){ showDiff(); });

  // Footer share to X
  var btnShareXFooter=document.getElementById('btn-share-x-footer');
  if(btnShareXFooter) btnShareXFooter.addEventListener('click',function(){ shareToX(); });

  // Keyboard help close button
  var btnKbClose=document.getElementById('btn-kb-close');
  if(btnKbClose) btnKbClose.addEventListener('click',function(){ toggleKbHelp(); });

  // Scroll-to-top button
  var scrollTopBtn=document.getElementById('scrollTop');
  if(scrollTopBtn) scrollTopBtn.addEventListener('click',function(){ window.scrollTo({top:0,behavior:'smooth'}); });

  // Table search input
  var tableSearch=document.getElementById('tableSearch');
  if(tableSearch){var _ftTimer;tableSearch.addEventListener('input',function(){clearTimeout(_ftTimer);_ftTimer=setTimeout(filterTable,200);});}
})();

// ── Sprint 4.2: Dynamic data refresh from dashboard-data.json ──
(function(){
  // 检查数据新鲜度（Sprint 4.3：>24h 显示过期警告）
  function checkDataFreshness(){
    var el=document.querySelector('[data-generated]');
    if(!el)return;
    var genStr=el.getAttribute('data-generated');
    if(!genStr)return;
    var genTime=new Date(genStr).getTime();
    if(isNaN(genTime))return;
    var ageHours=(Date.now()-genTime)/(1000*60*60);
    if(ageHours>24){
      var banner=document.createElement('div');
      banner.className='ah-stale-banner';
      banner.style.cssText='background:#f59e0b;color:#000;text-align:center;'
        +'padding:8px 16px;font-size:14px;font-weight:600;position:sticky;top:0;z-index:9999';
      banner.textContent='\u26a0\ufe0f \u6570\u636e\u53ef\u80fd\u5df2\u8fc7\u671f\uff08\u4e0a\u6b21\u66f4\u65b0: '
        +new Date(genTime).toLocaleString('zh-CN')+'\uff09';
      document.body.prepend(banner);
    }
  }

  // 尝试从 dashboard-data.json 动态刷新（如果可用）
  function fetchDashboardData(){
    fetch('dashboard-data.json?_t='+Date.now())
      .then(function(r){
        if(!r.ok)throw new Error('HTTP '+r.status);
        return r.json();
      })
      .then(function(data){
        if(!data||!data._generated_at)return;
        // 更新英雄区数字
        var heroDate=document.querySelector('.hero-date,.scan-date');
        if(heroDate&&data._date) heroDate.textContent=data._date;
        // 检查是否有更新的数据
        var genEl=document.querySelector('[data-generated]');
        if(genEl){
          var curGen=genEl.getAttribute('data-generated');
          if(curGen&&data._generated_at&&data._generated_at>curGen){
            console.log('[AH] \u53d1\u73b0\u65b0\u6570\u636e, \u5efa\u8bae\u5237\u65b0\u9875\u9762');
          }
        }
      })
      .catch(function(){
        // fetch \u5931\u8d25\u65f6\u964d\u7ea7\u4e3a\u5d4c\u5165\u6570\u636e\uff08\u9ed8\u8ba4\u884c\u4e3a\uff09
      });
  }

  // \u9875\u9762\u52a0\u8f7d\u540e\u6267\u884c
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',function(){
      checkDataFreshness();
      fetchDashboardData();
    });
  } else {
    checkDataFreshness();
    fetchDashboardData();
  }
})();
