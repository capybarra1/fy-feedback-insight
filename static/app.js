'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const currency = (value, digits = 2) => `¥${Number(value || 0).toFixed(digits)}`;
  const date = (value) => value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : '未知';
  const statuses = {pending:'待分析',done:'已分析',failed:'失败',unknown:'请求状态不明',skipped:'已跳过'};
  const reviews = {unreviewed:'未复核',needs_review:'待确认',reviewed:'已复核'};
  const relevanceNames = {unknown:'尚未判断',relevant:'相关',irrelevant:'不相关',uncertain:'待确认'};
  const brandNames = {related:'相关',unrelated:'不相关',uncertain:'待确认'};
  const scopeNames = {cockpit:'座舱',other:'非座舱',mixed:'座舱与其他混合',unknown:'无法判断',not_applicable:'不适用'};
  const contentNames = {product_feedback:'产品反馈',product_question:'产品咨询',promotion:'营销推广',delivery_chat:'交付闲聊',chitchat:'闲聊',other:'其他',uncertain:'待确认'};
  const feedbackNames = {fault:'故障',complaint:'抱怨',suggestion:'建议',question:'疑问',praise:'表扬',comparison:'对比',other:'其他'};
  const namedOptions = (names,selected) => Object.entries(names).map(([value,label]) => `<option value="${esc(value)}"${value === selected ? ' selected' : ''}>${esc(label)}</option>`).join('');
  const isV2 = opinion => opinion.schema_version === 'absa-v2';
  const reviewSchema = (opinions, previous = 'legacy') => opinions.length ? (opinions.every(isV2) ? 'absa-v2' : 'legacy') : previous;
  const submodules = module => options().submodules?.[module] || [];
  const colors = {'正面':'#e7ff5b','负面':'#c66b87','中性':'#b6a8eb','无法判断':'#c9cbd5'};
  const fallbackOptions = {modules:['智能硬件','辅助驾驶','语音助手（lumo）','车机','车辆其他功能','销售与服务','价格','交付','充电续航','轮胎底盘','无法判断','其他','待确认'],types:['体验评价','问题反馈','改进建议','咨询疑问','购买意向','对比评价','其他'],sentiments:['正面','负面','中性','无法判断']};
  let state = null, currentPage = 'dashboard', resultView = 'opinions', resultPage = 1, pendingFiles = [], estimate = null, activeSource = null, editors = [], settingsLoaded = false, pollBusy = false, modalDirty = false;
  const selectedKeys = new Set();
  const filters = {kind:'comment',module:'focus',sentiment:'',type:'',theme:'',review:'',from:'',to:'',q:'',batch:'',submodule:'',content_type:'',schema:'',needs_review:'',scope:''};
  const options = () => ({...fallbackOptions,...state?.options});
  const focusedModules = () => options().modules.slice(0,4);
  const optionHTML = (values, selected = '', emptyLabel = '') => `${emptyLabel ? `<option value="">${esc(emptyLabel)}</option>` : ''}${values.map((value) => `<option value="${esc(value)}"${value === selected ? ' selected' : ''}>${esc(value)}</option>`).join('')}`;
  const empty = (title, description, action = '') => `<div class="empty-state"><span class="empty-icon" aria-hidden="true">◌</span><strong>${esc(title)}</strong><p>${esc(description)}</p>${action}</div>`;
  function notify(message, error = false) {
    $('toast').textContent = message; $('toast').classList.toggle('error', error); $('toast').hidden = false;
    clearTimeout(notify.timer); notify.timer = setTimeout(() => {$('toast').hidden = true;}, 4500);
  }
  async function api(path, method = 'GET', data) {
    const response = await fetch(path, {method, headers:{...(method !== 'GET' ? {'Content-Type':'application/json','X-Firefly-Token':state?.csrf_token || ''} : {})}, ...(data === undefined ? {} : {body:JSON.stringify(data)})});
    let result;
    try { result = await response.json(); } catch { throw new Error('本地服务返回了无法读取的响应。'); }
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '操作未完成，请检查输入后重试。');
    return result;
  }
  async function refresh(initial = false) {
    if (pollBusy) return;
    pollBusy = true;
    try {
      state = await api('/api/state');
      $('connection-status').textContent = '本地已连接'; $('global-error').hidden = true;
      for (const key of selectedKeys) {if (!state.sources.some((source) => source.key === key && !source.manual && source.status !== 'unknown')) selectedKeys.delete(key);}
      populateFilters(); renderDashboard(); renderImports(); renderBudget();
      if (!settingsLoaded || initial) { populateSettings(); settingsLoaded = true; }
    } catch (error) {
      $('connection-status').textContent = '连接中断'; $('global-error').textContent = `${error.message} 请确认本地服务仍在运行，然后刷新页面。`; $('global-error').hidden = false;
    } finally { pollBusy = false; }
  }
  function showPage(page) {
    if (!['dashboard','imports','settings'].includes(page)) page = 'dashboard';
    currentPage = page;
    document.querySelectorAll('.page').forEach((el) => el.classList.toggle('active', el.id === `page-${page}`));
    document.querySelectorAll('[data-page]').forEach((el) => {el.classList.toggle('active', el.dataset.page === page); el.setAttribute('aria-current', el.dataset.page === page ? 'page' : 'false');});
    $('breadcrumb').textContent = `研究工作台 / ${{dashboard:'反馈看板',imports:'数据与分析',settings:'服务与预算'}[page]}`;
    if (location.hash !== `#${page}`) history.replaceState(null, '', `#${page}`);
  }
  function populateFilters() {
    $('filter-module').innerHTML = `<option value="focus">四个重点模块</option><option value="">全部模块</option>${optionHTML(options().modules)}`;
    const submoduleChoices = filters.module && filters.module !== 'focus' ? submodules(filters.module) : [...new Set(Object.values(options().submodules || {}).flat())];
    $('filter-submodule').innerHTML = optionHTML(submoduleChoices,filters.submodule,'全部子模块');
    $('filter-content_type').innerHTML = '<option value="">全部内容类型</option>' + namedOptions(contentNames,filters.content_type);
    $('filter-scope').innerHTML = '<option value="">全部范围</option>' + namedOptions(scopeNames,filters.scope);
    $('filter-type').innerHTML = optionHTML(options().types, filters.type, '全部类型');
    $('filter-sentiment').innerHTML = optionHTML(options().sentiments, filters.sentiment, '全部情感');
    $('filter-batch').innerHTML = `<option value="">全部批次</option>${state.batches.map((batch) => `<option value="${esc(batch.id)}">${esc(batch.filename)} · ${esc(date(batch.created_at))}</option>`).join('')}`;
    for (const [key,value] of Object.entries(filters)) {const el = $(`filter-${key}`); if (el && el.value !== value) el.value = value;}
  }
  function sourceMatches(source, search = true) {
    if (source.kind !== filters.kind) return false;
    if (filters.batch && !source.batch_ids.includes(filters.batch)) return false;
    if (filters.review && source.review_status !== filters.review) return false;
    if (filters.content_type && !(source.content_types || []).includes(filters.content_type)) return false;
    if (filters.schema && (source.analysis_schema || 'legacy') !== filters.schema) return false;
    if (filters.scope && source.product_scope !== filters.scope) return false;
    if (search && filters.needs_review && String(source.review_status === 'needs_review' || state.opinions.some(o => o.source_key === source.key && o.needs_review)) !== filters.needs_review) return false;
    const published = (source.published_at || '').slice(0,10);
    if ((filters.from || filters.to) && !published) return false;
    if (filters.from && published < filters.from) return false;
    if (filters.to && published > filters.to) return false;
    return !search || !filters.q || `${source.title || ''} ${source.text || ''}`.toLowerCase().includes(filters.q.toLowerCase());
  }
  function effectiveOpinions(ignoreModule = false) {
    const sourceMap = new Map(state.sources.map((source) => [source.key,source]));
    return state.opinions.filter((opinion) => {
      const source = sourceMap.get(opinion.source_key);
      if (!source || source.relevance !== 'relevant' || !(source.status === 'done' || source.manual === true || source.has_analysis === true) || !sourceMatches(source, false)) return false;
      if (source.eligible_for_insights === false) return false;
      if (filters.submodule && opinion.submodule !== filters.submodule) return false;
      if (filters.needs_review && String(Boolean(opinion.needs_review)) !== filters.needs_review) return false;
      if (!ignoreModule && filters.module && (filters.module === 'focus' ? !focusedModules().includes(opinion.module) : opinion.module !== filters.module)) return false;
      if (filters.sentiment && opinion.sentiment !== filters.sentiment || filters.type && opinion.type !== filters.type || filters.theme && opinion.theme !== filters.theme) return false;
      if (filters.q && !`${source.title || ''} ${source.text || ''} ${opinion.theme || ''}`.toLowerCase().includes(filters.q.toLowerCase())) return false;
      return true;
    }).map((opinion) => ({...opinion,source:sourceMap.get(opinion.source_key)}));
  }
  function renderDashboard() {
    if (!state) return;
    const kindSources = state.sources.filter((s) => s.kind === filters.kind);
    const scopedSources = state.sources.filter((s) => sourceMatches(s));
    const opinions = effectiveOpinions(), allModules = effectiveOpinions(true);
    $('comment-count').textContent = state.sources.filter((s) => s.kind === 'comment').length;
    $('note-count').textContent = state.sources.filter((s) => s.kind === 'note').length;
    $('scope-label').textContent = `按不同${filters.kind === 'comment' ? '评论' : '笔记'}统计讨论量 · 仅代表导入样本`;
    document.querySelectorAll('[data-kind]').forEach((el) => el.classList.toggle('active', el.dataset.kind === filters.kind));
    const overview = [
      ['导入来源',kindSources.length,filters.kind === 'comment' ? '评论及回复' : '笔记标题与正文'],
      ['已分析',kindSources.filter((s) => s.status === 'done').length,'含无观点与不相关内容'],
      ['待分析',kindSources.filter((s) => s.status === 'pending').length,'由你主动开始'],
      ['失败 / 状态不明',kindSources.filter((s) => ['failed','unknown'].includes(s.status)).length,'保留进度，可手动处理'],
      ['待确认',kindSources.filter((s) => s.review_status === 'needs_review').length,'原文与结果需要核对']
    ];
    $('overview-stats').innerHTML = overview.map(([title,value,caption]) => `<div class="stat"><div class="stat-label">${esc(title)}</div><div class="stat-value">${value}</div><div class="stat-caption">${esc(caption)}</div></div>`).join('');
    $('other-module-buttons').innerHTML = options().modules.slice(4).map((module) => `<button class="button secondary${filters.module === module ? ' active' : ''}" data-module="${esc(module)}">${esc(module)} <b>${new Set(allModules.filter((o) => o.module === module).map((o) => o.source_key)).size}</b> ↗</button>`).join('');
    const icons = ['▣','⌁','◉','▤'];
    $('module-grid').innerHTML = focusedModules().map((module, index) => {
      const list = allModules.filter((opinion) => opinion.module === module), count = new Set(list.map((opinion) => opinion.source_key)).size;
      const sentimentCounts = options().sentiments.map((sentiment) => [sentiment,list.filter((opinion) => opinion.sentiment === sentiment).length]);
      const positive = rankThemes(list.filter((o) => o.sentiment === '正面'))[0];
      const negative = rankThemes(list.filter((o) => o.sentiment === '负面'))[0];
      return `<button class="module-card${filters.module === module ? ' selected' : ''}" data-module="${esc(module)}" aria-label="筛选${esc(module)}"><div class="module-top"><span class="module-icon" aria-hidden="true">${icons[index]}</span><span class="module-index">0${index+1}</span></div><h3 class="module-title">${esc(module)}</h3><div class="module-amount">${count}<small>${filters.kind === 'comment' ? '条评论' : '篇笔记'}</small></div><div class="module-bar">${sentimentCounts.map(([sentiment,n]) => `<span style="width:${list.length ? n/list.length*100 : 0}%;background:${colors[sentiment] || colors['无法判断']}"></span>`).join('')}</div><div class="module-caption">${list.length ? `${list.length} 个观点 · ${list.filter((o) => o.sentiment === '负面').length} 个负面观点` : '暂无已分析观点'}</div>${positive || negative ? `<div class="module-highlight"><span class="positive">＋</span> ${esc(positive?.theme || '暂无正面亮点')}</div><div class="module-highlight"><span class="negative">−</span> ${esc(negative?.theme || '暂无负面主题')}</div>` : '<div class="module-highlights-empty">等待原文里的第一条声音</div>'}</button>`;
    }).join('');
    const legacyCount = state.sources.filter(s => s.has_analysis && (s.analysis_schema || 'legacy') === 'legacy').length;
    $('schema-banner').textContent = `ABSA v2 · 原文片段与标准子模块。${legacyCount} 条旧版分析原样保留，可在“数据与分析”中主动选择重分析；未重分析内容不补造抽取片段。第一阶段不推断父评论中的对象或态度。`;
    renderInsights(opinions);
    $('opinion-total').textContent = `${opinions.length} 个观点`;
    $('sentiment-chart').innerHTML = opinions.length ? `<div class="sentiment-summary"><strong>${opinions.length}</strong><span>有效观点 / 当前筛选</span></div><div class="sentiment-bar" aria-label="情感分布">${options().sentiments.map((sentiment) => `<span title="${esc(sentiment)}" style="width:${opinions.filter((o) => o.sentiment === sentiment).length/opinions.length*100}%;background:${colors[sentiment]}"></span>`).join('')}</div>${options().sentiments.map((sentiment) => {const count = opinions.filter((o) => o.sentiment === sentiment).length; return `<div class="sentiment-row"><span><i class="dot" style="background:${colors[sentiment]}"></i>${esc(sentiment)}</span><strong>${count}</strong><em>${Math.round(count/opinions.length*100)}%</em></div>`;}).join('')}` : empty('还没有可统计的观点','导入数据后，完成 AI 分析或人工整理，即可查看情感分布。');
    const themes = rankThemes(opinions);
    $('theme-list').innerHTML = themes.length ? themes.slice(0,6).map((theme,index) => `<button class="theme-row" data-theme="${esc(theme.theme)}"><span class="theme-rank">${String(index+1).padStart(2,'0')}</span><span class="theme-text"><strong>${esc(theme.theme || '未命名主题')}</strong><small>${filters.kind === 'comment' ? `涉及 ${theme.notes.size} 篇笔记 · ` : ''}${theme.opinions} 个观点</small></span><span class="theme-count">${theme.sources.size}</span><span class="theme-arrow">↗</span></button>`).join('') : empty('主题将在这里浮现','主题按不同来源排名，保留原文依据，不把单篇热帖当作普遍反馈。');
    $('clear-theme').hidden = !filters.theme;
    $('filtered-opinion-count').textContent = opinions.length; $('filtered-source-count').textContent = scopedSources.length;
    $('opinions-tab').classList.toggle('active', resultView === 'opinions'); $('sources-tab').classList.toggle('active', resultView === 'sources');
    $('library-description').textContent = resultView === 'sources' ? '包含待分析、失败、不相关及无观点来源；应用来源范围、内容类型、版本、复核状态与原文筛选；模块、子模块和情感仅筛选观点。' : `当前筛选共 ${opinions.length} 个有效观点；点击核对可查看上下文并修正。`;
    const records = resultView === 'opinions' ? opinions : scopedSources;
    const pages = Math.max(1,Math.ceil(records.length/12)); resultPage = Math.min(resultPage,pages);
    $('selection-actions').hidden = resultView !== 'sources';
    $('selection-actions').innerHTML = `<span>已选 ${selectedKeys.size} 条来源用于分析</span><div class="inline-actions"><button class="text-button" id="select-visible-button">选择本页可分析项</button><button class="text-button" id="clear-selection-button">清空</button><button class="button secondary" id="analyze-selection-button" ${selectedKeys.size ? '' : 'disabled'}>前往预估 ↗</button></div>`;
    $('results-list').innerHTML = records.length ? records.slice((resultPage-1)*12,resultPage*12).map((record) => renderResult(record,resultView)).join('') : empty(resultView === 'sources' ? '这里还没有符合条件的来源' : '当前范围还没有有效观点', state.sources.length ? '可以调整筛选，或打开“全部来源”查看待分析内容并进行人工整理。' : '先导入已采集的笔记与评论。分析只会在你主动开始后运行。', !state.sources.length ? '<button class="button primary" data-go="imports">导入第一批数据 ↗</button>' : '');
    $('pagination').innerHTML = records.length > 12 ? `<span>共 ${records.length} 条 · 第 ${resultPage} / ${pages} 页</span><button data-pagination="prev" ${resultPage === 1 ? 'disabled' : ''}>上一页</button><button data-pagination="next" ${resultPage === pages ? 'disabled' : ''}>下一页</button>` : `<span>${records.length ? `显示全部 ${records.length} 条` : '暂无记录'}</span>`;
    $('export-button').disabled = !opinions.length;
  }
  function rankThemes(opinions) {
    const map = new Map();
    for (const opinion of opinions) {
      if (!map.has(opinion.theme)) map.set(opinion.theme,{theme:opinion.theme,sources:new Set(),notes:new Set(),opinions:0});
      const theme = map.get(opinion.theme); theme.sources.add(opinion.source_key); if (opinion.source?.note_id) theme.notes.add(opinion.source.note_id); theme.opinions++;
    }
    return [...map.values()].sort((a,b) => b.sources.size-a.sources.size || a.theme.localeCompare(b.theme,'zh-CN'));
  }
  function renderSpans(opinion) {
    if (!isV2(opinion)) return '<p class="span-note">旧版观点 · 未记录对象与评价片段</p>';
    return `<dl class="opinion-spans"><div><dt>对象</dt><dd>${opinion.target_text == null ? '<span class="quiet">隐含对象 · null</span>' : esc(opinion.target_text)}</dd></div><div><dt>评价</dt><dd>${opinion.opinion_text == null ? '<span class="quiet">隐含评价 · null</span>' : esc(opinion.opinion_text)}</dd></div></dl><p class="span-note">${esc(opinion.aspect_category || [opinion.module,opinion.submodule].filter(Boolean).join(' / '))}${opinion.context_used ? ' · 借助笔记确认产品背景' : ' · 未使用上下文'}${opinion.needs_review ? ' · 需要人工复核' : ''}</p>`;
  }
  function renderRouting(source) {
    if ((source.routing_schema || source.analysis_schema || 'legacy') === 'legacy') return '<p class="span-note">旧版来源 · 未补造来源路由</p>';
    return `<div class="routing-tags"><span class="tag">${esc(brandNames[source.brand_relevance] || '未标注')}</span><span class="tag">${esc(scopeNames[source.product_scope] || '未标注')}</span>${(source.content_types || []).map(type => `<span class="tag">${esc(contentNames[type] || type)}</span>`).join('')}${source.eligible_for_insights === false ? '<span class="tag warning">不纳入观点统计</span>' : ''}</div>${source.filter_reason ? `<p class="span-note">过滤原因：${esc(source.filter_reason)}</p>` : ''}${source.review_reasons?.length ? `<p class="span-note">复核原因：${source.review_reasons.map(esc).join('；')}</p>` : ''}`;
  }
  function insightSummary(opinions) {
    const sources = new Set(opinions.map(o => o.source_key));
    const negativeSources = new Set(opinions.filter(o => o.sentiment === '负面').map(o => o.source_key));
    return {sources:sources.size,negativeSources:negativeSources.size,negativeShare:sources.size ? negativeSources.size/sources.size : null};
  }
  function timelineRows(opinions) {
    const buckets = new Map();
    for (const opinion of opinions) {
      const month = (opinion.source?.published_at || '').slice(0,7) || '日期未知';
      if (!buckets.has(month)) buckets.set(month,[]);
      buckets.get(month).push(opinion);
    }
    return [...buckets].sort(([a],[b]) => a.localeCompare(b)).map(([month,list]) => ({month,...insightSummary(list),opinions:list.length}));
  }
  function renderInsights(opinions) {
    const summary = insightSummary(opinions), issues = rankThemes(opinions.filter(o => o.sentiment === '负面'));
    $('insight-denominator').textContent = `当前筛选含有效观点的 ${summary.sources} 条来源为分母；${summary.negativeSources} 条至少含一个负面观点（${summary.negativeShare == null ? '—' : `${(summary.negativeShare*100).toFixed(1)}%`}）。同一来源去重，仅代表导入样本。`;
    $('issue-list').innerHTML = issues.length ? issues.slice(0,5).map(issue => {
      const evidence = opinions.find(o => o.theme === issue.theme && o.sentiment === '负面');
      return `<article class="issue-row"><button class="text-button" data-theme="${esc(issue.theme)}">${esc(issue.theme)} ↗</button><span>${issue.sources.size} / ${summary.sources} 条来源</span><blockquote>${esc(evidence?.evidence)}</blockquote><button class="text-button" data-source="${esc(evidence?.source_key)}">核对代表性证据</button></article>`;
    }).join('') : empty('暂无负面议题','当前筛选内尚无负面观点。');
    const rows = timelineRows(opinions);
    $('timeline-list').innerHTML = rows.length ? `<div class="timeline-table"><table><caption>每月以当月含有效观点的来源为分母；含负面观点即计入负面来源，不代表用户赞同。</caption><thead><tr><th>发布月份</th><th>样本来源</th><th>负面来源 / 占比</th><th>观点</th></tr></thead><tbody>${rows.map(row => `<tr><td>${esc(row.month)}</td><td>${row.sources}</td><td>${row.negativeSources} / ${(row.negativeShare*100).toFixed(1)}%</td><td>${row.opinions}</td></tr>`).join('')}</tbody></table></div>` : empty('时间趋势等待数据','按来源发布时间汇总；缺失时间会单列。');
  }
  function renderMetrics() {
    const m = state.analysis_metrics || {}, percent = value => value == null ? '—' : `${(Number(value)*100).toFixed(1)}%`, seconds = value => value == null ? '—' : `${Number(value).toFixed(2)} 秒`;
    const metrics = [['请求数',m.requests ?? 0],['请求成功率',percent(m.request_success_rate)],['JSON 成功率',percent(m.json_success_rate)],['截断率',percent(m.truncation_rate)],['格式修复率',percent(m.retry_rate)],['平均耗时',seconds(m.avg_latency_seconds)],['P95 耗时',seconds(m.p95_latency_seconds)],['记录费用',currency(m.cost,4)]];
    $('analysis-metrics').innerHTML = metrics.map(([label,value]) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');
    $('metrics-note').textContent = `${Number(m.legacy_requests || 0)} 条历史请求缺少新遥测，不推算成功率。— 表示尚无可统计数据；用量不明的预留费用仍保留。`;
  }
  function renderResult(record, view) {
    const source = view === 'opinions' ? record.source : record;
    const title = view === 'opinions' ? record.theme || '未命名主题' : source.title || (source.kind === 'comment' ? '评论 / 回复' : '未命名笔记');
    const content = view === 'opinions' ? record.evidence : source.text;
    const tags = view === 'opinions' ? `<span class="tag">${esc(record.module)}${record.submodule ? ` / ${esc(record.submodule)}` : ''}</span><span class="tag ${record.sentiment === '负面' ? 'negative' : record.sentiment === '正面' ? 'positive' : ''}">${esc(record.sentiment)}</span><span class="tag">${esc(feedbackNames[record.feedback_type] || record.type)}</span>${record.needs_review ? '<span class="tag warning">观点待复核</span>' : ''}<span class="tag">${isV2(record) ? 'ABSA v2' : '旧版'}</span>` : `<span class="tag${['unknown','failed'].includes(source.status) ? ' warning' : ''}">${esc(statuses[source.status] || source.status)}</span><span class="tag">${esc(relevanceNames[source.relevance] || source.relevance)}</span>`;
    return `<article class="result-row"><div><div class="result-title">${view === 'sources' ? `<label class="source-select"><input type="checkbox" data-select-source="${esc(source.key)}" ${selectedKeys.has(source.key) ? 'checked' : ''} ${source.manual || source.status === 'unknown' ? 'disabled' : ''} aria-label="选择此来源用于分析"></label>` : ''}${esc(title)}</div><div class="result-text">${esc(content)}</div>${view === 'opinions' ? renderSpans(record) : renderRouting(source)}<div class="row-meta"><span>${source.kind === 'comment' ? '评论' : '笔记'} #${esc(source.external_id)}</span><span>发布 ${esc(date(source.published_at))}</span>${source.manual ? '<span>人工整理</span>' : ''}${source.duplicate_of ? '<span class="tag warning">疑似重复</span>' : ''}<span class="${source.review_status === 'needs_review' ? 'tag warning' : ''}">${esc(reviews[source.review_status] || source.review_status)}</span></div></div><div class="row-tags">${tags}</div><button class="row-action" data-source="${esc(source.key)}">核对 ↗</button></article>`;
  }
  function renderImports() {
    const job = state.job || {}, running = Boolean(job.running);
    $('job-status').innerHTML = `<div class="job-state"><strong>${running ? '正在分析中' : job.status === 'idle' || !job.status ? '等待你的下一步' : esc(({completed:'分析已完成',done:'分析已完成',stopped:'已停止派发',stopping:'正在停止',failed:'分析遇到异常',error:'分析遇到异常',paused:'分析已暂停',finished:'分析已完成'})[job.status] || job.status)}</strong><p>${esc(job.message || '不会在导入后自动发起调用。')}</p>${job.total ? `<div class="progress-track"><i style="width:${Math.min(100,Number(job.completed || 0)/Number(job.total)*100)}%"></i></div><p>${Number(job.completed || 0)} / ${Number(job.total)} 条已处理 · 成功 ${Number(job.succeeded || 0)} / 失败 ${Number(job.failed || 0)} / 跳过 ${Number(job.skipped || 0)}</p>` : ''}</div>`;
    $('analysis-counts').innerHTML = [['待分析','pending'],['已完成','done'],['失败','failed'],['已跳过','skipped']].map(([name,status]) => `<div><strong>${state.sources.filter((source) => source.status === status).length}</strong><span>${name}</span></div>`).join('');
    const unknown = state.sources.filter((s) => s.status === 'unknown');
    if (unknown.length) $('job-status').innerHTML += `<div class="notice danger">${unknown.length} 条请求状态不明，可能已产生费用。请在“全部来源”中核对并确认后重试，不会自动重复提交。</div>`;
    $('estimate-button').disabled = running; $('analysis-mode').disabled = running; $('stop-button').hidden = !running;
    $('selection-summary').hidden = $('analysis-mode').value !== 'selected';
    $('selection-summary').textContent = `已选 ${selectedKeys.size} 条。可在反馈看板的“全部来源”列表选择需要分析的内容。人工结果与状态不明请求不可直接重分析。`;
    $('batch-count').textContent = `${state.batches.length} 个批次`;
    $('batch-list').innerHTML = state.batches.length ? state.batches.map((batch) => `<article class="batch-row"><div class="batch-header"><div><div class="batch-name">${esc(batch.filename)}</div><div class="batch-meta">${esc(date(batch.created_at))} · ${esc(batch.site)} · ${Number(batch.count || 0)} 行</div></div><button class="text-button" data-delete-batch="${esc(batch.id)}">删除批次</button></div><div class="batch-stats"><span>新增<b>${Number(batch.new || 0)}</b></span><span>更新<b>${Number(batch.updated || 0)}</b></span><span>重复<b>${Number(batch.duplicate || 0)}</b></span><span>异常<b>${Number(batch.invalid || 0)}</b></span></div>${batch.errors?.length ? `<details><summary>查看 ${batch.errors.length} 条异常详情</summary><ul>${batch.errors.map((error) => `<li>第 ${esc(error.line)} 行：${esc(error.message)}</li>`).join('')}</ul></details>` : ''}</article>`).join('') : empty('尚未导入任何批次','选择 MediaCrawler 导出的 JSONL 文件，开始建立你的反馈资料库。');
  }
  function renderBudget() {
    const budget = state.budget || {}, settings = state.settings || {};
    $('side-remaining').textContent = currency(budget.remaining); $('side-budget-caption').textContent = `已用 ${currency(budget.spent)} · 预留 ${currency(budget.reserved)}`;
    $('side-budget-bar').style.width = `${Math.max(0,Math.min(100,Number(settings.monthly_budget) ? Number(budget.remaining)/Number(settings.monthly_budget)*100 : 0))}%`;
    $('budget-month').textContent = `${budget.month || '本月'} 费用`;
    $('budget-summary').innerHTML = `<div class="budget-big">${currency(budget.remaining)} <small>可用</small></div><div class="budget-breakdown"><div><span>月预算</span><strong>${currency(settings.monthly_budget)}</strong></div><div><span>已结算</span><strong>${currency(budget.spent,4)}</strong></div><div><span>预留中</span><strong>${currency(budget.reserved,4)}</strong></div></div>`;
    renderMetrics();
    $('ledger-list').innerHTML = state.ledger.length ? state.ledger.slice().reverse().map((entry) => `<div class="ledger-row"><div><span>${esc({reserved:'费用已预留',settled:'已结算',unknown:'用量不明，保留费用'}[entry.status] || entry.status)}</span><p>${esc(date(entry.created_at))} · ${esc(entry.source_key || '来源已删除')}</p>${entry.outcome ? `<p>${esc(entry.outcome)} · ${esc(entry.finish_reason || '结束原因未记录')} · ${entry.duration_ms == null ? '耗时未记录' : `${(Number(entry.duration_ms)/1000).toFixed(2)} 秒`} · 修复轮次 ${Number(entry.retry_index || 0)}</p>` : ''}${entry.prompt_tokens != null || entry.completion_tokens != null ? `<p>输入 ${Number(entry.prompt_tokens || 0)} / 输出 ${Number(entry.completion_tokens || 0)} token · ${esc(entry.model || '')} · ${esc(entry.rule_version || '')}</p>` : ''}${entry.error ? `<p>${esc(entry.error)}</p>` : ''}</div><strong>${currency(entry.status === 'settled' ? entry.amount : entry.reserved,4)}</strong></div>`).join('') : empty('还没有费用记录','只有主动启动分析才会发出请求。');
  }
  function populateSettings() {
    for (const key of ['base_url','model','input_price','output_price','monthly_budget','max_output_tokens','max_input_chars','concurrency','reasoning_effort','format_retries']) $(`setting-${key}`).value = state.settings[key] ?? ({concurrency:2,reasoning_effort:'low',format_retries:1}[key] ?? '');
    $('setting-api_key').value = ''; $('setting-clear_key').checked = false;
    $('key-status').textContent = state.settings.has_api_key ? (state.settings.key_source === 'env' ? '· 已从 .env 加载' : '· 本次运行已设置') : '· 尚未设置';
    $('env-file-path').textContent = state.settings.env_file || '重启本地服务后显示文件位置';
  }
  function invalidateEstimate() {estimate = null; $('estimate-panel').hidden = true; $('estimate-panel').innerHTML = '';}
  function renderFiles() {
    $('selected-files').innerHTML = pendingFiles.map((file,index) => `<div class="file-row"><span>${esc(file.name)} · ${(file.size/1024).toFixed(1)} KB</span><button data-remove-file="${index}" aria-label="移除 ${esc(file.name)}">×</button></div>`).join('');
    $('import-button').disabled = !pendingFiles.length;
  }
  async function importFiles() {
    if (!pendingFiles.length) return;
    if (pendingFiles.length > 20 || pendingFiles.reduce((sum,file) => sum + file.size,0) > 30000000) {notify('单次最多 20 个文件，总计不超过 30 MB，请分批导入。',true); return;}
    $('import-button').disabled = true; $('import-button').textContent = '正在导入…';
    try {
      const files = await Promise.all(pendingFiles.map(async (file) => ({name:file.name,content:await file.text()})));
      const result = await api('/api/import','POST',{files,site:$('import-site').value});
      const totals = result.totals;
      $('import-result').textContent = `导入完成：新增 ${totals.new}、更新 ${totals.updated}、重复 ${totals.duplicate}、异常 ${totals.invalid}。未自动启动分析。`;
      $('import-result').hidden = false; pendingFiles = []; $('file-input').value = ''; renderFiles(); invalidateEstimate(); await refresh(); notify('数据已导入并保存在本机。');
    } catch (error) {notify(error.message,true);} finally {$('import-button').textContent = '导入文件'; $('import-button').disabled = !pendingFiles.length;}
  }
  async function estimateAnalysis() {
    $('estimate-button').disabled = true;
    try {
      estimate = await api('/api/analysis/estimate','POST',{mode:$('analysis-mode').value,...($('analysis-mode').value === 'selected' ? {source_keys:[...selectedKeys]} : {})});
      $('estimate-panel').hidden = false;
      const preview = estimate.preview || {};
      $('estimate-panel').innerHTML = `<div class="estimate-box"><span class="quiet">本次 ${Number(estimate.count)} 条 · 纯噪声跳过 ${Number(estimate.noise_count || 0)} 条 · 预计费用上限</span><div class="estimate-price">${currency(estimate.estimated_cost,4)}</div><p>${esc(estimate.reason || '确认后才会向你配置的服务发送内容。')}</p>${estimate.excluded?.length ? `<details><summary>已排除 ${estimate.excluded.length} 条内容</summary>${estimate.excluded.map((item) => `<p>${esc(item.key)}：${esc(item.reason)}</p>`).join('')}</details>` : ''}<details><summary>查看本次外发内容示例</summary><pre>${esc(preview.source_text || '暂无待发送内容')}</pre><strong>仅用于确认产品的笔记上下文</strong><pre>${esc(typeof preview.brand_context === 'string' ? preview.brand_context : JSON.stringify(preview.brand_context || '没有可用上下文'))}</pre><p>所有对象、评价和证据均来自当前原文，不借用父评论推断。</p></details><button id="start-analysis-button" class="button primary" ${estimate.can_start && estimate.estimate_token ? '' : 'disabled'}>确认费用，开始分析</button><p>预估已包含配置允许的格式修复费用上限。停止仅阻止后续派发，已经发出的请求仍可能计费。</p></div>`;
    } catch (error) {notify(error.message,true);} finally {$('estimate-button').disabled = Boolean(state.job.running);}
  }
  async function startAnalysis() {
    if (!estimate?.can_start || !estimate?.estimate_token) return;
    $('start-analysis-button').disabled = true;
    try {await api('/api/analysis/start','POST',{estimate_token:estimate.estimate_token}); invalidateEstimate(); await refresh(); notify('分析已开始，可以随时停止后续派发。');} catch (error) {invalidateEstimate(); notify(error.message,true);}
  }
  async function deleteBatch(id) {
    const sources = state.sources.filter((source) => source.batch_ids.includes(id));
    const exclusive = sources.filter((source) => source.batch_ids.length === 1).length;
    if (!confirm(`确定删除这个导入批次？\n\n将删除仅属于此批次的 ${exclusive} 条来源及其分析结果。与其他批次共享的 ${sources.length-exclusive} 条来源会保留。费用历史始终保留，原始导入文件不会更改。`)) return;
    try {const result = await api(`/api/batches/${encodeURIComponent(id)}`,'DELETE'); invalidateEstimate(); if (filters.batch === id) filters.batch = ''; await refresh(); notify(`已删除 ${result.deleted_sources} 条独有来源，保留 ${result.retained_sources} 条共享来源。`);} catch (error) {notify(error.message,true);}
  }
  async function saveSettings(event) {
    event.preventDefault(); const button = $('settings-form').querySelector('button[type=submit]'); button.disabled = true;
    const payload = {};
    for (const key of ['base_url','model','reasoning_effort']) payload[key] = $(`setting-${key}`).value.trim();
    for (const key of ['input_price','output_price','monthly_budget','max_output_tokens','max_input_chars','concurrency','format_retries']) payload[key] = $(`setting-${key}`).value === '' && ['input_price','output_price'].includes(key) ? null : Number($(`setting-${key}`).value);
    if ($('setting-api_key').value) payload.api_key = $('setting-api_key').value;
    if ($('setting-clear_key').checked) payload.clear_key = true;
    try {await api('/api/settings','PUT',payload); $('setting-api_key').value = ''; invalidateEstimate(); await refresh(); populateSettings(); $('settings-feedback').textContent = '设置已保存'; notify('设置已保存，分析前请重新预估费用。');} catch (error) {$('settings-feedback').textContent = error.message; notify(error.message,true);} finally {button.disabled = false;}
  }
  async function openSource(key) {
    if (modalDirty && !confirm('放弃尚未保存的修改？')) return;
    modalDirty = false; activeSource = null; $('source-dialog-content').innerHTML = empty('正在读取本地原文','请稍候…');
    if (!$('source-dialog').open) $('source-dialog').showModal();
    try {
      activeSource = await api(`/api/sources/${encodeURIComponent(key)}`);
      editors = activeSource.opinions.map((opinion) => ({...opinion}));
      renderSource();
    } catch (error) {$('source-dialog-content').innerHTML = `<div class="modal-body"><div class="notice danger">${esc(error.message)}</div></div>`;}
  }
  function safeSourceURL(url) {try {const parsed = new URL(url); return ['http:','https:'].includes(parsed.protocol) ? parsed.href : '';} catch {return '';}}
  function renderSource() {
    const {source,context,history:historyItems,url} = activeSource;
    const safeURL = safeSourceURL(url);
    $('source-dialog-content').innerHTML = `<div class="modal-body"><h3>${esc(source.title || (source.kind === 'comment' ? '评论 / 回复' : '笔记原文'))}</h3><div class="source-meta"><span>${source.kind === 'comment' ? '评论' : '笔记'} #${esc(source.external_id)}</span><span>${esc(source.site)}</span><span>${esc(statuses[source.status])}</span>${safeURL ? `<a href="${esc(safeURL)}" target="_blank" rel="noopener noreferrer">打开平台原文 ↗</a>` : ''}</div><div class="source-original">${esc(source.text || '（没有文本）')}</div>${source.has_analysis && source.analysis_version != null && Number(source.analysis_version) !== Number(source.version) ? `<div class="notice">正文已更新到版本 ${Number(source.version)}，下方观点仍来自版本 ${Number(source.analysis_version)}。旧结果保留至新分析或人工核对完成；保存人工结果时，证据需与当前版本原文一致。</div><details class="context-list" open><summary>当前观点对应的旧版原文</summary><div class="source-original previous-original">${esc(source.analysis_text || '旧版文本不可用')}</div></details>` : ''}<div class="source-meta"><span>发布 ${esc(date(source.published_at))}</span><span>首次导入 ${esc(date(source.first_seen))}</span><span>最近采集 ${esc(date(source.collected_at))}</span><span>版本 ${Number(source.version || 1)}</span></div>${source.error ? `<div class="notice danger">${esc(source.error)}</div>` : ''}${source.status === 'unknown' ? `<div class="notice danger">这条请求状态不明，可能已产生费用。确认重试会保留原费用预留，并允许再次调用。</div><button id="retry-confirm-button" class="button danger-button">我已核对，允许重新尝试</button>` : ''}${source.duplicate_of ? `<div class="notice">疑似与 ${esc(source.duplicate_of)} 重复。仅提示，不会自动删除。</div>` : ''}<details class="context-list"><summary>查看关联内容（${context?.length || 0} 条；不作为观点证据）</summary>${context?.length ? context.map((item) => `<div class="context-entry"><strong>${item.kind === 'note' ? '所属笔记' : '上级评论'}${item.title ? ` · ${esc(item.title)}` : ''}</strong>\n${esc(item.text)}</div>`).join('') : '<p class="helper-text">尚未导入可用上下文，不代表平台上没有相关内容。</p>'}</details><div class="review-header"><div><h2>人工核对</h2><p class="helper-text">修改仅影响当前来源，历史版本会保留。</p></div><button id="add-opinion-button" class="button secondary">＋ 添加观点</button></div><div class="review-controls"><label>与萤火虫汽车的相关性<select id="review-relevance">${Object.entries(relevanceNames).map(([value,label]) => `<option value="${value}" ${source.relevance === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label><label>复核状态<select id="review-status">${Object.entries(reviews).map(([value,label]) => `<option value="${value}" ${source.review_status === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label></div>${renderRoutingEditor(source)}<div id="opinion-editors"></div><details class="history-list"><summary>原始 / AI / 人工历史（${historyItems?.length || 0}）</summary>${historyItems?.length ? historyItems.map(renderHistory).join('') : '<p class="helper-text">暂无历史版本。</p>'}</details><div id="review-error" class="notice danger" hidden></div><div class="modal-footer"><p>证据必须逐字来自此条原文。保存为人工结果后，后续分析不会自动覆盖。只有标记为“相关”的有效观点才纳入看板统计。</p><button id="save-review-button" class="button primary">保存核对结果</button></div></div>`;
    renderEditors();
  }
  function renderHistory(item) {
    const snapshot = item.snapshot || {}, oldSource = snapshot.source || {}, result = snapshot.result || {}, opinions = snapshot.opinions || result.opinions || [];
    const title = {content_updated:'正文更新前的版本',manual_review:'人工修改前的结果',ai_result:'AI 分析结果'}[item.event] || '历史记录';
    return `<article class="history-entry"><strong>${esc(title)}</strong><span>${esc(date(item.created_at))}</span>${oldSource.text ? `<div class="context-entry">${esc(oldSource.text)}</div>` : ''}${oldSource.relevance || result.relevance ? `<p class="helper-text">相关性：${esc(relevanceNames[oldSource.relevance || result.relevance] || '未知')}${oldSource.review_status ? ` · ${esc(reviews[oldSource.review_status])}` : ''}</p>` : ''}${opinions.length ? opinions.map((opinion) => `<div class="history-opinion"><div>${esc(opinion.module)} · ${esc(opinion.type)} · ${esc(opinion.sentiment)}</div><strong>${esc(opinion.theme)}</strong><p>${esc(opinion.evidence)}</p>${renderSpans(opinion)}</div>`).join('') : '<p class="helper-text">此版本没有观点。</p>'}</article>`;
  }
  function renderRoutingEditor(source) {
    return `<details class="routing-editor" open><summary>来源路由与复核原因 · ${(source.routing_schema || source.analysis_schema) === 'absa-v2' ? 'ABSA v2' : '旧版保留'}</summary><div class="review-controls"><label>品牌相关性<select id="review-brand"><option value="">旧版未标注</option>${namedOptions(brandNames,source.brand_relevance)}</select></label><label>产品范围<select id="review-scope"><option value="">旧版未标注</option>${namedOptions(scopeNames,source.product_scope)}</select></label></div><fieldset><legend>内容类型（可多选）</legend><div class="routing-checks">${Object.entries(contentNames).map(([value,label]) => `<label class="checkbox-label"><input type="checkbox" data-content-type="${esc(value)}" ${(source.content_types || []).includes(value) ? 'checked' : ''}>${esc(label)}</label>`).join('')}</div></fieldset><label>复核原因（每行一项）<textarea id="review-reasons">${esc((source.review_reasons || []).join('\n'))}</textarea></label>${source.filter_reason ? `<p class="helper-text">过滤原因：${esc(source.filter_reason)}</p>` : ''}<p class="helper-text">${source.eligible_for_insights === false ? '此来源当前未纳入观点统计；修正路由和观点后由服务重新判定。' : '保留原文，通过路由区分产品反馈与其他内容。'}品牌相关性与原有相关性应保持一致。</p></details>`;
  }
  function renderEditors() {
    const mixedNotice = editors.some(isV2) && !editors.every(isV2) ? '<div class="notice subtle">含旧版观点，整条来源仍保留旧版标记；补全所有观点后可转为 ABSA v2。</div>' : '';
    $('opinion-editors').innerHTML = mixedNotice + (editors.length ? editors.map((opinion,index) => {
      const v2 = isV2(opinion), subs = submodules(opinion.module);
      return `<section class="opinion-editor" data-editor="${index}"><div class="opinion-editor-header"><span>观点 ${String(index+1).padStart(2,'0')} · ${v2 ? 'ABSA v2' : '旧版'}</span><div>${v2 ? '' : `<button class="text-button" data-upgrade-opinion="${index}">补全为 v2</button>`}<button class="text-button" data-remove-opinion="${index}">删除观点</button></div></div>${v2 ? '' : '<p class="helper-text">旧版没有对象与评价片段，保持原样保存不会补造数据。</p>'}<div class="opinion-fields"><label>模块<select data-field="module">${optionHTML(v2 ? options().modules.filter(m => !['其他','待确认'].includes(m)) : options().modules,opinion.module)}</select></label>${v2 ? `<label>标准子模块<select data-field="submodule">${optionHTML(subs,opinion.submodule,'请选择子模块')}</select></label><label>反馈类型<select data-field="feedback_type">${namedOptions(options().feedback_types || feedbackNames,opinion.feedback_type)}</select></label>` : `<label>反馈类型<select data-field="type">${optionHTML(options().types,opinion.type)}</select></label>`}<label>情感<select data-field="sentiment">${optionHTML(options().sentiments,opinion.sentiment)}</select></label></div><label>具体主题<input data-field="theme" maxlength="120" value="${esc(opinion.theme)}" placeholder="v2 留空时使用标准子模块"></label>${v2 ? `<div class="span-editors"><label>评价对象原文<input data-field="target_text" value="${esc(opinion.target_text)}" ${opinion.implicit_target ? 'disabled' : ''} placeholder="逐字片段；隐含对象留空"></label><label class="checkbox-label"><input type="checkbox" data-field="implicit_target" ${opinion.implicit_target ? 'checked' : ''}>隐含对象（保存为 null）</label><label>评价表达原文<input data-field="opinion_text" value="${esc(opinion.opinion_text)}" ${opinion.implicit_opinion ? 'disabled' : ''} placeholder="逐字片段；隐含评价留空"></label><label class="checkbox-label"><input type="checkbox" data-field="implicit_opinion" ${opinion.implicit_opinion ? 'checked' : ''}>隐含评价（保存为 null）</label></div><div class="routing-checks"><label class="checkbox-label"><input type="checkbox" data-field="context_used" disabled>上下文推断关闭（第一阶段）</label><label class="checkbox-label"><input type="checkbox" data-field="needs_review" ${opinion.needs_review ? 'checked' : ''}>观点需要复核</label></div><p class="helper-text">隐含片段不补写；不使用父评论推断对象、评价或用户赞同。</p>` : ''}<label>原文证据<textarea data-field="evidence" placeholder="粘贴此条原文中的准确片段">${esc(opinion.evidence)}</textarea></label></section>`;
    }).join('') : '<div class="notice subtle">当前没有观点。可添加观点，也可以在确认没有明确反馈后直接保存相关性与复核状态。</div>');
  }
  function collectEditors() {
    document.querySelectorAll('[data-editor]').forEach((el) => {const index = Number(el.dataset.editor); el.querySelectorAll('[data-field]').forEach((input) => {editors[index][input.dataset.field] = input.type === 'checkbox' ? input.checked : input.value;});});
  }
  function prepareOpinion(opinion) {
    const result = {module:opinion.module,type:opinion.type,sentiment:opinion.sentiment,theme:(opinion.theme || '').trim(),evidence:(opinion.evidence || '').trim(),schema_version:isV2(opinion) ? 'absa-v2' : 'legacy'};
    if (isV2(opinion)) Object.assign(result,{submodule:opinion.submodule,aspect_category:`${opinion.module}-${opinion.submodule}`,theme:result.theme || opinion.submodule,feedback_type:opinion.feedback_type,sentiment_code:({'正面':'positive','负面':'negative','中性':'neutral','无法判断':'uncertain'}[opinion.sentiment] || 'uncertain'),target_text:opinion.implicit_target ? null : (opinion.target_text || '').trim(),opinion_text:opinion.implicit_opinion ? null : (opinion.opinion_text || '').trim(),implicit_target:Boolean(opinion.implicit_target),implicit_opinion:Boolean(opinion.implicit_opinion),context_used:false,needs_review:Boolean(opinion.needs_review || opinion.implicit_opinion || opinion.module === '无法判断' || opinion.sentiment === '无法判断')});
    return result;
  }
  function validateOpinion(opinion, sourceText) {
    if (!opinion.theme || !opinion.evidence || !sourceText.includes(opinion.evidence)) return '每条观点都需主题与证据；证据必须逐字来自当前原文。';
    if (!isV2(opinion)) return '';
    if (!submodules(opinion.module).includes(opinion.submodule)) return '请选择当前模块下的标准子模块。';
    for (const [field,implicit] of [['target_text','implicit_target'],['opinion_text','implicit_opinion']]) {
      if (opinion[implicit] ? opinion[field] !== null : !opinion[field] || !opinion.evidence.includes(opinion[field])) return '对象和评价表达必须是证据中的逐字片段；隐含项请勾选并留空。';
    }
    return '';
  }
  async function saveReview() {
    collectEditors(); const source = activeSource.source, opinions = editors.map(prepareOpinion);
    const invalid = opinions.map(opinion => validateOpinion(opinion,source.text || '')).find(Boolean);
    if (invalid) {$('review-error').textContent = invalid; $('review-error').hidden = false; return;}
    const payload = {relevance:$('review-relevance').value,review_status:$('review-status').value,opinions};
    const brand = $('review-brand').value, scope = $('review-scope').value;
    if (brand) payload.brand_relevance = brand;
    if (scope) payload.product_scope = scope;
    payload.content_types = [...document.querySelectorAll('[data-content-type]:checked')].map(input => input.dataset.contentType);
    payload.review_reasons = $('review-reasons').value.split('\n').map(value => value.trim()).filter(Boolean);
    payload.analysis_schema = reviewSchema(opinions,source.analysis_schema || 'legacy');
    if (brand === 'uncertain') opinions.filter(isV2).forEach(opinion => {opinion.needs_review = true;});
    let routingError = '';
    if (payload.analysis_schema === 'absa-v2') {
      if (!brand || !scope || !payload.content_types.length) routingError = '请补全品牌相关性、产品范围和至少一种内容类型。';
      else if ((brand === 'uncertain' || opinions.some(opinion => opinion.needs_review)) && !payload.review_reasons.length) routingError = '品牌或观点需要复核时，请填写复核原因。';
    }
    if (routingError) {$('review-error').textContent = routingError; $('review-error').hidden = false; return;}
    $('save-review-button').disabled = true;
    try {
      await api(`/api/sources/${encodeURIComponent(source.key)}/review`,'PUT',payload);
      modalDirty = false; invalidateEstimate(); await refresh(); await openSource(source.key); notify('人工核对结果已保存，看板同步更新。');
    } catch (error) {$('review-error').textContent = error.message; $('review-error').hidden = false; $('save-review-button').disabled = false;}
  }
  function closeSource() {if (modalDirty && !confirm('放弃尚未保存的修改？')) return; modalDirty = false; $('source-dialog').close();}
  function resetFilters() {Object.assign(filters,{module:'focus',sentiment:'',type:'',theme:'',review:'',from:'',to:'',q:'',batch:'',submodule:'',content_type:'',schema:'',needs_review:'',scope:''}); resultPage = 1; populateFilters(); renderDashboard();}
  function bindEvents() {
    document.querySelectorAll('[data-page]').forEach((el) => el.addEventListener('click',() => showPage(el.dataset.page)));
    window.addEventListener('hashchange',() => showPage(location.hash.slice(1)));
    document.querySelectorAll('[data-kind]').forEach((el) => el.addEventListener('click',() => {filters.kind = el.dataset.kind; resultPage = 1; renderDashboard();}));
    for (const key of Object.keys(filters)) {
      if (key === 'kind') continue;
      const el = $(`filter-${key}`);
      el.addEventListener(['q','theme'].includes(key) ? 'input' : 'change',() => {filters[key] = el.value; if (key === 'module') {filters.submodule = ''; populateFilters();} resultPage = 1; renderDashboard();});
    }
    $('reset-filters').addEventListener('click',resetFilters);
    $('clear-theme').addEventListener('click',() => {filters.theme = ''; $('filter-theme').value = ''; resultPage = 1; renderDashboard();});
    $('opinions-tab').addEventListener('click',() => {resultView = 'opinions'; resultPage = 1; renderDashboard();});
    $('sources-tab').addEventListener('click',() => {resultView = 'sources'; resultPage = 1; renderDashboard();});
    $('export-button').addEventListener('click',() => {const params = new URLSearchParams(Object.entries(filters).filter(([,value]) => value !== '')); const a = document.createElement('a'); a.href = `/api/export.csv?${params}`; a.download = 'firefly-feedback.csv'; a.click();});
    document.addEventListener('click',async (event) => {
      const target = event.target.closest('button'); if (!target) return;
      if (target.dataset.module) {filters.module = target.dataset.module; filters.submodule = ''; populateFilters(); resultPage = 1; renderDashboard();}
      if (target.dataset.theme !== undefined) {filters.theme = target.dataset.theme; $('filter-theme').value = filters.theme; resultPage = 1; renderDashboard();}
      if (target.dataset.source) await openSource(target.dataset.source);
      if (target.dataset.go) showPage(target.dataset.go);
      if (target.dataset.pagination) {resultPage += target.dataset.pagination === 'next' ? 1 : -1; renderDashboard();}
      if (target.dataset.removeFile !== undefined) {pendingFiles.splice(Number(target.dataset.removeFile),1); renderFiles();}
      if (target.dataset.deleteBatch) await deleteBatch(target.dataset.deleteBatch);
      if (target.dataset.removeOpinion !== undefined) {collectEditors(); editors.splice(Number(target.dataset.removeOpinion),1); modalDirty = true; renderEditors();}
      if (target.id === 'select-visible-button') {state.sources.filter((source) => sourceMatches(source)).slice((resultPage-1)*12,resultPage*12).filter((source) => !source.manual && source.status !== 'unknown').forEach((source) => selectedKeys.add(source.key)); invalidateEstimate(); renderDashboard(); renderImports();}
      if (target.id === 'clear-selection-button') {selectedKeys.clear(); invalidateEstimate(); renderDashboard(); renderImports();}
      if (target.id === 'analyze-selection-button') {$('analysis-mode').value = 'selected'; invalidateEstimate(); renderImports(); showPage('imports');}
      if (target.id === 'add-opinion-button') {if (editors.length >= 30) {notify('单条来源最多 30 个观点。',true); return;} collectEditors(); editors.push({schema_version:'absa-v2',module:'无法判断',submodule:'',feedback_type:'other',sentiment:'无法判断',theme:'',evidence:'',target_text:null,opinion_text:null,implicit_target:true,implicit_opinion:true,context_used:false,needs_review:true}); modalDirty = true; renderEditors();}
      if (target.dataset.upgradeOpinion !== undefined) {collectEditors(); const opinion = editors[Number(target.dataset.upgradeOpinion)]; Object.assign(opinion,{schema_version:'absa-v2',module:focusedModules().includes(opinion.module) ? opinion.module : '无法判断',submodule:'',feedback_type:'other',target_text:null,opinion_text:null,implicit_target:true,implicit_opinion:true,context_used:false,needs_review:true}); modalDirty = true; renderEditors();}
      if (target.id === 'save-review-button') await saveReview();
      if (target.id === 'start-analysis-button') await startAnalysis();
      if (target.id === 'retry-confirm-button') {
        if (!confirm('此前请求可能已经计费，重新分析可能再次产生费用。确认保留原费用并将此来源设为可手动重试？')) return;
        try {await api(`/api/sources/${encodeURIComponent(activeSource.source.key)}/retry-confirm`,'POST',{}); modalDirty = false; invalidateEstimate(); await refresh(); await openSource(activeSource.source.key); notify('已设为失败待重试，请到数据与分析页面重新预估。');} catch (error) {notify(error.message,true);}
      }
    });
    $('file-input').addEventListener('change',() => {pendingFiles = [...pendingFiles,...$('file-input').files]; renderFiles();});
    $('dropzone').addEventListener('keydown',(event) => {if (['Enter',' '].includes(event.key)) {event.preventDefault(); $('file-input').click();}});
    ['dragenter','dragover'].forEach((name) => $('dropzone').addEventListener(name,(event) => {event.preventDefault(); $('dropzone').classList.add('dragging');}));
    ['dragleave','drop'].forEach((name) => $('dropzone').addEventListener(name,(event) => {event.preventDefault(); $('dropzone').classList.remove('dragging');}));
    $('dropzone').addEventListener('drop',(event) => {pendingFiles = [...pendingFiles,...event.dataTransfer.files]; renderFiles();});
    $('import-button').addEventListener('click',importFiles); $('estimate-button').addEventListener('click',estimateAnalysis); $('analysis-mode').addEventListener('change',() => {invalidateEstimate(); renderImports();});
    $('stop-button').addEventListener('click',async () => {try {await api('/api/analysis/stop','POST',{}); await refresh(); notify('已请求停止派发，正在进行的请求仍会结算。');} catch (error) {notify(error.message,true);}});
    document.addEventListener('change',(event) => {const input = event.target.closest('[data-select-source]'); if (!input) return; if (input.checked) selectedKeys.add(input.dataset.selectSource); else selectedKeys.delete(input.dataset.selectSource); invalidateEstimate(); renderDashboard(); renderImports();});
    $('settings-form').addEventListener('submit',saveSettings);
    $('close-dialog').addEventListener('click',closeSource);
    $('source-dialog').addEventListener('cancel',(event) => {event.preventDefault(); closeSource();});
    $('source-dialog').addEventListener('input',() => {modalDirty = true;});
    $('source-dialog').addEventListener('change',(event) => {modalDirty = true; const field = event.target.dataset.field; if (['module','implicit_target','implicit_opinion'].includes(field)) {collectEditors(); const opinion = editors[Number(event.target.closest('[data-editor]').dataset.editor)]; if (field === 'module') opinion.submodule = ''; if (field === 'implicit_target' && opinion.implicit_target) opinion.target_text = null; if (field === 'implicit_opinion' && opinion.implicit_opinion) opinion.opinion_text = null; renderEditors();} if (event.target.id === 'review-relevance') $('review-brand').value = {relevant:'related',irrelevant:'unrelated',uncertain:'uncertain',unknown:''}[event.target.value] || ''; if (event.target.id === 'review-brand' && event.target.value) $('review-relevance').value = {related:'relevant',unrelated:'irrelevant',uncertain:'uncertain'}[event.target.value];});
    window.addEventListener('beforeunload',(event) => {if (modalDirty) {event.preventDefault(); event.returnValue = '';}});
  }
  bindEvents(); showPage(location.hash.slice(1) || 'dashboard');
  refresh(true);
  setInterval(() => {if (state?.job?.running && !document.hidden) refresh();},2500);
})();
