/* Meteor Earth Track engine (shared by AZ Sat Catcher and sat.dahabra.com).
   Works out, from live orbit data, the strip of Earth each Meteor pass photographs while the station hears it.
   Needs d3, topojson, satellite.js and window.ET_WORLD. Call ET.start(cfg). */
(function(){
"use strict";
const ET = window.ET = {};
const R_KM = 6371, HALF_SWATH = 1400;          // MSU-MR: ~2,800 km wide
const TZ = {timeZone:"Asia/Dubai"};
const fmtT = d => new Date(d).toLocaleTimeString("en-GB",{...TZ,hour:"2-digit",minute:"2-digit"});
const fmtTs = d => new Date(d).toLocaleTimeString("en-GB",{...TZ,hour:"2-digit",minute:"2-digit",second:"2-digit"});
const fmtD = d => new Date(d).toLocaleDateString("en-GB",{...TZ,weekday:"long",day:"numeric",month:"short"});
const mins = ms => Math.max(0,Math.round(ms/60000));
const human = m => m<60? `${m} min` : `${Math.floor(m/60)} h ${m%60} min`;
const deg = Math.PI/180;
const $ = id => document.getElementById(id);

/* ---------- orbit maths ---------- */
function parseTLE(text){
  const L = text.split(/\r?\n/).map(s=>s.trimEnd()).filter(Boolean), out = {};
  for(let i=0;i<L.length-1;i++){
    if(L[i].startsWith("1 ") && L[i+1].startsWith("2 ")){
      const num = L[i].substr(2,5), name = (i>0 && !L[i-1].startsWith("2 ")) ? L[i-1].trim() : "";
      const id = num==="57166"||/M2[ -]?3$/.test(name) ? "M2-3" : num==="59051"||/M2[ -]?4$/.test(name) ? "M2-4" : null;
      if(id){ try{ out[id] = {rec:satellite.twoline2satrec(L[i],L[i+1]), epoch:L[i].substr(18,14)}; }catch(e){} }
      i++;
    }
  }
  return out;
}
function sub(rec,t){
  const pv = satellite.propagate(rec,new Date(t)); if(!pv||!pv.position) return null;
  const g = satellite.eciToGeodetic(pv.position, satellite.gstime(new Date(t)));
  return [ +(satellite.degreesLong(g.longitude)).toFixed(3), +(satellite.degreesLat(g.latitude)).toFixed(3) ];
}
function look(rec,t,obs){
  const d = new Date(t), pv = satellite.propagate(rec,d); if(!pv||!pv.position) return null;
  const ecf = satellite.eciToEcf(pv.position, satellite.gstime(d));
  const la = satellite.ecfToLookAngles({latitude:obs.lat*deg,longitude:obs.lon*deg,height:(obs.h||0)}, ecf);
  return {el:la.elevation/deg, az:(la.azimuth/deg+360)%360};
}
function findPasses(rec,from,to,obs,mask){
  const out=[]; const step=30e3; let prev=look(rec,from,obs), up = prev && prev.el>=mask, rise = up?from:null;
  const edge=(a,b,goingUp)=>{ for(let k=0;k<20;k++){ const m=(a+b)/2, e=look(rec,m,obs).el>=mask; if(e===goingUp) b=m; else a=m; } return b; };
  for(let t=from+step;t<=to;t+=step){
    const l = look(rec,t,obs); if(!l) continue; const u = l.el>=mask;
    if(u && !up) rise = edge(t-step,t,true);
    if(!u && up && rise!=null){ const stop = edge(t-step,t,false); out.push({rise,stop}); rise=null; }
    up = u;
  }
  return out;
}
function subsolar(t){
  const d=new Date(t), n=(t-Date.UTC(d.getUTCFullYear(),0,1))/864e5;
  const decl=-23.44*Math.cos(2*Math.PI/365*(n+10)), hrs=d.getUTCHours()+d.getUTCMinutes()/60;
  return [(12-hrs)*15, decl];
}
function sunEl(t,pt){
  const s=subsolar(t); return 90 - d3.geoDistance(pt,s)/deg;
}
function bearing(a,b){
  const [lo1,la1,lo2,la2]=[a[0]*deg,a[1]*deg,b[0]*deg,b[1]*deg];
  return Math.atan2(Math.sin(lo2-lo1)*Math.cos(la2), Math.cos(la1)*Math.sin(la2)-Math.sin(la1)*Math.cos(la2)*Math.cos(lo2-lo1))/deg;
}
function dest(p,brg,km){
  const la=p[1]*deg, lo=p[0]*deg, b=brg*deg, dr=km/R_KM;
  const la2=Math.asin(Math.sin(la)*Math.cos(dr)+Math.cos(la)*Math.sin(dr)*Math.cos(b));
  const lo2=lo+Math.atan2(Math.sin(b)*Math.sin(dr)*Math.cos(la),Math.cos(dr)-Math.sin(la)*Math.sin(la2));
  return [((lo2/deg+540)%360)-180, la2/deg];
}
function buildPass(id,rec,rise,stop,obs,extra){
  const p = Object.assign({sat:id, A:rise, L:stop, rec}, extra||{});
  p.dur = Math.max(1,Math.round((stop-rise)/60000));
  // centre line every 10 s while heard
  const c=[]; for(let t=rise;t<stop;t+=10e3){ const s=sub(rec,t); if(s) c.push(s); } const e=sub(rec,stop); if(e) c.push(e);
  p.center = c;
  // grey: 25 min either side
  p.before=[]; for(let t=rise-25*60e3;t<=rise;t+=20e3){ const s=sub(rec,t); if(s) p.before.push(s); } p.before.push(c[0]);
  p.after=[c[c.length-1]]; for(let t=stop;t<=stop+25*60e3;t+=20e3){ const s=sub(rec,t); if(s) p.after.push(s); }
  // swath strip
  const Ls=[],Rs=[];
  c.forEach((pt,i)=>{ const a=c[Math.max(0,i-1)], b=c[Math.min(c.length-1,i+1)], br=bearing(a,b);
    Ls.push(dest(pt,br-90,HALF_SWATH)); Rs.push(dest(pt,br+90,HALF_SWATH)); });
  let ring = Ls.concat(Rs.reverse(), [Ls[0]]);
  p.swath = {type:"Polygon",coordinates:[ring]};
  if(d3.geoArea(p.swath) > 2*Math.PI) p.swath.coordinates[0] = ring.slice().reverse();
  // max elevation, side, direction, daylight
  let best={el:-90,t:rise}; for(let t=rise;t<=stop;t+=15e3){ const l=look(rec,t,obs); if(l&&l.el>best.el) best={el:l.el,t,az:l.az}; }
  if(p.maxel==null) p.maxel = +best.el.toFixed(1);
  p.side = best.az!=null ? (best.az<180?"east":"west") : (p.side||"");
  p.dir = c[c.length-1][1] > c[0][1] ? "northbound" : "southbound";
  p.day = sunEl((rise+stop)/2, c[Math.floor(c.length/2)]) > 5;
  return p;
}

/* ---------- page ---------- */
ET.start = function(cfg){
  const COL = cfg.colors || {"M2-3":"#5ee1e6","M2-4":"#e8b45c"};
  const NAME = {"M2-3":"Meteor-M N2-3","M2-4":"Meteor-M N2-4"};
  const short = s => s.replace("M2-","N2-");
  let obs = cfg.obs, mask = cfg.mask||10, PASSES=[], sats={}, sel=null, showAll=false, filt=new Set(["M2-3","M2-4"]), scrubT=null, followLive=true;
  const now = () => Date.now();

  // hearing circle radius on the ground for the mask elevation (satellite ~830 km up)
  const hearDeg = () => { const h=830, e=mask*deg; return (Math.acos(R_KM*Math.cos(e)/(R_KM+h)) - e)/deg; };

  /* globe */
  const svg = d3.select("#etGlobe");
  const proj = d3.geoOrthographic().clipAngle(90).precision(.3);
  const path = d3.geoPath(proj);
  const land = topojson.feature(ET_WORLD, ET_WORLD.objects.countries);
  const borders = topojson.mesh(ET_WORLD, ET_WORLD.objects.countries, (a,b)=>a!==b);
  const grat = d3.geoGraticule10();
  const defs = svg.append("defs");
  const g1 = defs.append("radialGradient").attr("id","etOcean").attr("cx","38%").attr("cy","34%");
  g1.append("stop").attr("offset","0%").attr("stop-color", cfg.ocean1||"#16325a");
  g1.append("stop").attr("offset","100%").attr("stop-color", cfg.ocean2||"#081428");
  const g2 = defs.append("radialGradient").attr("id","etGlow");
  g2.append("stop").attr("offset","80%").attr("stop-color","#5ee1e6").attr("stop-opacity",.16);
  g2.append("stop").attr("offset","100%").attr("stop-color","#5ee1e6").attr("stop-opacity",0);
  const stars = svg.append("g"), glow = svg.append("circle").attr("fill","url(#etGlow)");
  const sphere = svg.append("path").attr("fill","url(#etOcean)");
  const gGrat = svg.append("path").attr("fill","none").attr("stroke","#1d3356").attr("stroke-width",.6);
  const gLand = svg.append("path").attr("fill","#2a3d33").attr("stroke","#3d5747").attr("stroke-width",.5);
  const gBord = svg.append("path").attr("fill","none").attr("stroke","#4b6656").attr("stroke-width",.35);
  const gNight = svg.append("path").attr("fill","rgba(0,0,8,.5)");
  const gHear = svg.append("path").attr("fill","rgba(255,92,122,.06)").attr("stroke","#ff5c7a").attr("stroke-width",1.4).attr("stroke-dasharray","5 4");
  const gOther = svg.append("g"), gSel = svg.append("g"), gHome = svg.append("g"), gSat = svg.append("g");
  let W,H,zoomK=1;
  function resize(){
    const box = document.querySelector(".etGlobeWrap").getBoundingClientRect();
    W=box.width; H=box.height; const Rr=Math.min(W,H)*0.44*zoomK;
    svg.attr("viewBox",`0 0 ${W} ${H}`);
    proj.translate([W/2,H/2-12]).scale(Rr);
    glow.attr("cx",W/2).attr("cy",H/2-12).attr("r",Rr*1.12);
    stars.selectAll("*").remove(); const rnd=d3.randomLcg(7);
    for(let i=0;i<140;i++) stars.append("circle").attr("cx",rnd()*W).attr("cy",rnd()*H).attr("r",rnd()*1.1+.2).attr("fill","#fff").attr("opacity",rnd()*.5+.1);
    draw();
  }
  const visible = pt => { const r=proj.rotate(); return d3.geoDistance(pt,[-r[0],-r[1]]) < Math.PI/2; };
  const line = c => ({type:"LineString",coordinates:c});
  function drawPass(g,p,strong){
    const c=COL[p.sat];
    g.append("path").datum(p.swath).attr("d",path).attr("fill",c)
      .attr("fill-opacity", strong ? (p.day?.30:.16) : .09)
      .attr("stroke",c).attr("stroke-opacity",strong?.85:.45).attr("stroke-width",strong?1.3:.8).attr("stroke-dasharray",p.day?null:"3 3");
    if(strong){
      [p.before,p.after].forEach(seg=>g.append("path").datum(line(seg)).attr("d",path).attr("fill","none").attr("stroke","#7d8698").attr("stroke-width",1.8).attr("stroke-dasharray","5 4"));
    }
    g.append("path").datum(line(p.center)).attr("d",path).attr("fill","none").attr("stroke",c).attr("stroke-width",strong?3:1.8).attr("opacity",strong?1:.55);
    if(!strong) return;
    const s=p.center, a=s[s.length-2], b=s[s.length-1];
    if(a&&visible(b)){ const pa=proj(a),pb=proj(b),ang=Math.atan2(pb[1]-pa[1],pb[0]-pa[0])/deg;
      g.append("path").attr("d","M-7,-5 L4,0 L-7,5 Z").attr("fill",c).attr("transform",`translate(${pb})rotate(${ang})`); }
    [[s[0],"start "+fmtT(p.A)],[s[s.length-1],"end "+fmtT(p.L)]].forEach(([pt,txt])=>{
      if(!visible(pt)) return; const xy=proj(pt);
      g.append("circle").attr("cx",xy[0]).attr("cy",xy[1]).attr("r",3.2).attr("fill","#fff").attr("stroke",c).attr("stroke-width",1.5);
      g.append("text").attr("x",xy[0]+7).attr("y",xy[1]+4).text(txt).attr("fill","#fff").attr("font-size",11).attr("font-weight",600).attr("paint-order","stroke").attr("stroke","#070b14").attr("stroke-width",3);
    });
  }
  function draw(){
    sphere.attr("d",path({type:"Sphere"})); gGrat.attr("d",path(grat)); gLand.attr("d",path(land)); gBord.attr("d",path(borders));
    const ss=subsolar(scrubT ?? (sel?sel.A:now()));
    gNight.attr("d",path(d3.geoCircle().center([ss[0]+180,-ss[1]]).radius(90)()));
    gHear.attr("d",path(d3.geoCircle().center([obs.lon,obs.lat]).radius(hearDeg())()));
    gOther.selectAll("*").remove(); gSel.selectAll("*").remove(); gHome.selectAll("*").remove(); gSat.selectAll("*").remove();
    if(showAll) PASSES.filter(p=>p!==sel && filt.has(p.sat) && p.L>now()).forEach(p=>drawPass(gOther,p,false));
    if(sel && filt.has(sel.sat)) drawPass(gSel,sel,true);
    const H0=[obs.lon,obs.lat];
    if(visible(H0)){ const h=proj(H0);
      gHome.append("circle").attr("cx",h[0]).attr("cy",h[1]).attr("r",9).attr("fill","none").attr("stroke","#ff5c7a").attr("opacity",.5);
      gHome.append("circle").attr("cx",h[0]).attr("cy",h[1]).attr("r",4.5).attr("fill","#ff5c7a").attr("stroke","#fff").attr("stroke-width",1.5);
      gHome.append("text").attr("x",h[0]+12).attr("y",h[1]-8).text(obs.name||"Station").attr("fill","#ffd0da").attr("font-size",12).attr("font-weight",600).attr("paint-order","stroke").attr("stroke","#070b14").attr("stroke-width",3); }
    if(sel && scrubT!=null){ const pt=sub(sel.rec,scrubT);
      if(pt && visible(pt)){ const xy=proj(pt), c=COL[sel.sat];
        gSat.append("circle").attr("cx",xy[0]).attr("cy",xy[1]).attr("r",11).attr("fill",c).attr("opacity",.25);
        gSat.append("g").attr("transform",`translate(${xy})`).html(`<rect x="-3" y="-3" width="6" height="6" fill="#fff"/><rect x="-13" y="-2" width="8" height="4" fill="${c}"/><rect x="5" y="-2" width="8" height="4" fill="${c}"/>`); } }
  }
  let r0,m0,anim=null;
  const stopAnim=()=>{ if(anim){anim.stop();anim=null;} };
  svg.call(d3.drag().on("start",e=>{r0=proj.rotate();m0=[e.x,e.y];stopAnim();})
    .on("drag",e=>{const k=70/proj.scale();proj.rotate([r0[0]+(e.x-m0[0])*k*1.2,Math.max(-90,Math.min(90,r0[1]-(e.y-m0[1])*k*1.2)),0]);draw();}));
  svg.on("wheel",e=>{e.preventDefault();zoomK=Math.max(.7,Math.min(3,zoomK*(e.deltaY<0?1.1:.9)));resize();},{passive:false});
  function spinTo(ll){ stopAnim(); const from=proj.rotate(), to=[-ll[0],-ll[1],0]; let d=to[0]-from[0]; d=((d+540)%360)-180; to[0]=from[0]+d;
    const it=d3.interpolate(from,to); anim=d3.timer(el=>{const k=Math.min(1,el/900);proj.rotate(it(d3.easeCubicInOut(k)));draw();if(k>=1)stopAnim();}); }

  /* list */
  function renderCards(){
    const box=$("etCards"); box.innerHTML=""; let lastDay="";
    const list=PASSES.filter(p=>filt.has(p.sat) && p.L>now()-30*60e3);
    if(!list.length){ box.innerHTML=`<div class="etEmpty">No passes found in the next ${cfg.hours||36} hours. What to do: check the orbit data is fresh (see the note below), then reload.</div>`; return; }
    list.forEach(p=>{
      const d=fmtD(p.A); if(d!==lastDay){lastDay=d; box.insertAdjacentHTML("beforeend",`<div class="etDay">${d}</div>`);}
      const t=now(), done=p.L<t, live=p.A<=t&&t<=p.L;
      const until= live?`<b class="etLive">Happening now · ${human(mins(p.L-t))} left</b>`: done?"Just passed":`Starts in ${human(mins(p.A-t))}`;
      const el=document.createElement("div");
      el.className="etCard"+(sel===p?" sel":"")+(done?" done":"");
      el.style.setProperty("--c",COL[p.sat]);
      const plan = p.would ? `<span class="etTag ${p.would==='skip'?'':'etPlan'}" title="${(p.why||'').replace(/"/g,'&quot;')}">Catcher: ${p.would}</span>` : "";
      el.innerHTML=`<div class="etT1"><span class="etSat">${short(p.sat)}</span><span class="etTime">${fmtT(p.A)} – ${fmtT(p.L)}</span></div>
        <div class="etMeta">${p.day?'<span class="etTag etSun">☀ Daylight · colour</span>':'<span class="etTag etMoon">☾ Night · infrared</span>'}
        <span class="etTag">${Math.round(p.maxel)}° high</span><span class="etTag">${p.dur} min</span>
        <span class="etTag">${p.dir==="northbound"?"↑ going north":"↓ going south"}</span>
        ${p.day&&p.maxel>=30?'<span class="etTag etBest">★ Best pick</span>':''}${plan}</div>
        <div class="etWhen">${until}${p.side?` · passes to the ${p.side} of you`:''}</div>
        <div class="etBar"><div style="width:${Math.min(100,p.maxel/90*100)}%"></div></div>`;
      el.onclick=()=>{followLive=false;select(p,true);};
      box.appendChild(el);
    });
  }
  function info(p){
    const ns=p.dir==="northbound";
    $("etInfo").innerHTML=`<b style="color:${COL[p.sat]}">${NAME[p.sat]}</b> · ${fmtD(p.A)}, ${fmtT(p.A)} to ${fmtT(p.L)} (${p.dur} min).<br>
      It comes in from the ${ns?"south":"north"} and leaves to the ${ns?"north":"south"}, about ${Math.round(p.maxel)}° above your horizon at its highest${p.side?`, off to your ${p.side}`:''}.<br>
      ${p.day?"The sun is up along this strip, so expect a full colour picture.":"This strip is in darkness, so expect infrared (heat) pictures only: clouds show, land detail is faint."}
      ${p.why?`<br><span style="opacity:.8">Catcher's plan: ${p.would}, ${p.why}</span>`:''}`;
  }
  function select(p,spin){
    sel=p; const t=now(); scrubT = (p.A<=t&&t<=p.L)? t : p.A; syncSlider();
    if(spin){ const mid=p.center[Math.floor(p.center.length/2)]; spinTo([(mid[0]+obs.lon)/2+(mid[0]-obs.lon)*.3,(mid[1]+obs.lat)/2]); }
    renderCards(); info(p); updateScrub();
  }
  /* scrubber */
  const slider=$("etSlider");
  const syncSlider=()=>{ if(sel) slider.value=Math.round(1000*(scrubT-sel.A)/(sel.L-sel.A)); };
  function updateScrub(){
    if(!sel) return;
    $("etScrubName").innerHTML=`<b style="color:${COL[sel.sat]}">${short(sel.sat)}</b> · ${fmtT(sel.A)}–${fmtT(sel.L)}`;
    const t=now(), live=sel.A<=t&&t<=sel.L;
    $("etScrubT").textContent=(live&&Math.abs(scrubT-t)<3e3?"Live: satellite at ":"Satellite at ")+fmtTs(scrubT);
    const left=Math.max(0,sel.L-scrubT);
    $("etScrubLeft").innerHTML=`<b>${Math.floor(left/60000)}:${String(Math.floor(left/1000)%60).padStart(2,"0")}</b> left in pass`;
    draw();
  }
  slider.oninput=()=>{ if(!sel)return; scrubT=sel.A+(sel.L-sel.A)*slider.value/1000; stopPlay(); updateScrub(); };
  let playT=null;
  function stopPlay(){ if(playT){playT.stop();playT=null;$("etPlay").textContent="▶ Play pass";} }
  $("etPlay").onclick=()=>{ if(!sel)return; if(playT){stopPlay();return;}
    $("etPlay").textContent="❚❚ Pause"; let v=+slider.value>=1000?0:+slider.value;
    playT=d3.interval(()=>{v+=4;if(v>=1000){v=1000;stopPlay();}slider.value=v;scrubT=sel.A+(sel.L-sel.A)*v/1000;updateScrub();},40); };
  document.querySelectorAll(".etChip[data-f]").forEach(ch=>ch.onclick=()=>{
    const f=ch.dataset.f; if(filt.has(f)&&filt.size>1){filt.delete(f);ch.classList.remove("on");} else {filt.add(f);ch.classList.add("on");}
    if(sel&&!filt.has(sel.sat)){const n=PASSES.find(p=>filt.has(p.sat)&&p.L>now()); if(n) select(n,true);} renderCards(); draw(); });
  $("etAll").onclick=e=>{showAll=!showAll;e.currentTarget.classList.toggle("on",showAll);draw();};
  $("etHome").onclick=()=>spinTo([obs.lon,obs.lat]);
  window.addEventListener("resize",resize);

  /* data */
  async function load(){
    const t=now();
    let tleText="", planned=null;
    try{ tleText = await cfg.getTLE(); }catch(e){}
    if(!tleText || !tleText.includes("\n1 ")) tleText = cfg.fallbackTLE || "";
    sats = parseTLE(tleText);
    if(cfg.getPlan){ try{ planned = await cfg.getPlan(); }catch(e){ planned=null; } }
    if(cfg.getObs){ try{ const o=await cfg.getObs(); if(o&&isFinite(o.lat)) obs=o; }catch(e){} }
    const list=[];
    if(planned && planned.length){
      planned.forEach(q=>{ const s=sats[q.sat]; if(!s) return; if(q.set*1000 < t-30*60e3) return;
        list.push(buildPass(q.sat,s.rec,q.rise*1000,q.set*1000,obs,{maxel:q.max_el,would:q.would,why:q.why})); });
    } else {
      Object.entries(sats).forEach(([id,s])=>findPasses(s.rec,t-20*60e3,t+(cfg.hours||36)*3600e3,obs,mask).forEach(q=>list.push(buildPass(id,s.rec,q.rise,q.stop,obs))));
    }
    list.sort((a,b)=>a.A-b.A); PASSES=list;
    const ep = Object.entries(sats).map(([id,s])=>{ const y=+s.epoch.slice(0,2), doy=+s.epoch.slice(2); const ms=Date.UTC(2000+y,0,1)+(doy-1)*864e5; return `${short(id)} ${new Date(ms).toLocaleDateString("en-GB",{...TZ,day:"numeric",month:"short"})}`; });
    const age = Math.max(...Object.values(sats).map(s=>{const y=+s.epoch.slice(0,2),doy=+s.epoch.slice(2);return (t-(Date.UTC(2000+y,0,1)+(doy-1)*864e5))/864e5;}));
    $("etSrc").innerHTML = (Object.keys(sats).length? `Orbit data: ${ep.join(", ")}${planned&&planned.length?" · passes from the catcher's own plan (with your sky mask)":` · passes counted from ${mask}° up`}.` : "No orbit data. What to do: check the station has internet, then reload.")
      + (age>4? ` <b class="etWarn">Orbit data is ${Math.round(age)} days old, so times may drift. What to do: ${cfg.staleFix||"reload later"}.</b>`:"");
    if($("etWhere")) $("etWhere").textContent = obs.name || "";
    // keep the selection if it's still there, else follow the live/next pass
    const keep = sel && PASSES.find(p=>p.sat===sel.sat && Math.abs(p.A-sel.A)<120e3);
    const next = PASSES.find(p=>p.L>t);
    if(keep && !followLive) select(keep,false); else if(next) select(next, !sel);
    else { renderCards(); draw(); }
  }
  // live tick: countdowns, and follow the satellite during a pass
  let tick=0;
  setInterval(()=>{
    const t=now(); tick++;
    if(followLive && sel && sel.A<=t && t<=sel.L && !playT){ scrubT=t; syncSlider(); updateScrub(); }
    if(followLive && sel && t>sel.L+60e3){ const n=PASSES.find(p=>p.L>t); if(n) select(n,true); }
    if(tick%15===0) renderCards();
    if($("etClock")) $("etClock").textContent = "Dubai " + fmtT(t);
  }, 1000);
  setInterval(load, 10*60e3);
  proj.rotate([-obs.lon,-obs.lat,0]); if(document.querySelector('.etGlobeWrap').getBoundingClientRect().width<560) zoomK=1.35; resize();
  if($("etClock")) $("etClock").textContent = "Dubai " + fmtT(now());
  load();
};
})();
