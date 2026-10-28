/* Offline Markdown and math. Untrusted content never enables raw HTML. */
(() => {
  const escape = value => String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  function math(value, display) {
    try {
      if (value.length > 4000) throw new Error('公式过长');
      return katex.renderToString(value, {displayMode:display, trust:false, maxExpand:500,
        maxSize:20, throwOnError:true, macros:{}});
    } catch (_) { return `<code class="math-error" title="公式需要核对">${escape(value)}</code>`; }
  }
  function safeExternal(value) {
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase().replace(/^\[|\]$/g,'');
      return url.protocol === 'https:' && !url.username && !url.password && (!url.port || url.port === '443')
        && host.includes('.') && !/^(localhost|127\.|10\.|192\.168\.|169\.254\.|0\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host)
        && !/\.(localhost|local|internal)$/.test(host);
    } catch (_) { return false; }
  }
  window.RichText = {render(source, lessonId = '', revision = '') {
    const md = markdownit({html:false, linkify:false, typographer:false});
    md.inline.ruler.before('escape','math_inline',(state,silent) => {
      const pos = state.pos;
      if (state.src[pos] !== '$' || state.src[pos+1] === '$' || /\s/.test(state.src[pos+1] || ' ')) return false;
      let end = pos+1;
      while ((end = state.src.indexOf('$',end)) !== -1 && state.src[end-1] === '\\') end++;
      if (end === -1 || /\s/.test(state.src[end-1]) || state.src.slice(pos,end).includes('\n')) return false;
      if (!silent) { const token=state.push('math_inline','math',0); token.content=state.src.slice(pos+1,end); }
      state.pos=end+1; return true;
    });
    md.block.ruler.before('fence','math_block',(state,start,end,silent) => {
      const first=state.src.slice(state.bMarks[start]+state.tShift[start],state.eMarks[start]).trim();
      if (!first.startsWith('$$')) return false;
      if (silent) return true;
      let value=first.slice(2), next=start+1;
      if (value.endsWith('$$')) value=value.slice(0,-2);
      else {
        let closed=false;
        for (;next<end;next++) {
          const line=state.src.slice(state.bMarks[next]+state.tShift[next],state.eMarks[next]);
          if (line.trim().endsWith('$$')) { value+='\n'+line.slice(0,line.lastIndexOf('$$')); next++; closed=true; break; }
          value+='\n'+line;
        }
        if (!closed) return false;
      }
      const token=state.push('math_block','math',0); token.block=true; token.content=value.trim();
      state.line=next; return true;
    },{alt:['paragraph','reference','blockquote','list']});
    md.renderer.rules.math_inline = (tokens,index) => math(tokens[index].content,false);
    md.renderer.rules.math_block = (tokens,index) => `<div class="math-block">${math(tokens[index].content,true)}</div>`;
    md.renderer.rules.image = (tokens,index) => {
      const token=tokens[index], src=token.attrGet('src') || '', alt=token.content || '图片';
      if (safeExternal(src)) return `<button type="button" class="remote-image" data-remote-image="${escape(src)}" data-alt="${escape(alt)}">加载外部图片：${escape(alt)}（将连接来源网站）</button>${lessonId ? `<button type="button" class="btn btn-line btn-sm" data-cache-image="${escape(src)}">缓存用于离线导出</button>` : ''}`;
      if (lessonId && /^(assets\/)([a-zA-Z0-9_.-]+\/)*[a-zA-Z0-9_.-]+\.(png|jpg|jpeg|webp)$/i.test(src)) {
        const path=src.split('/').map(encodeURIComponent).join('/');
        return `<img src="/api/lessons/${encodeURIComponent(lessonId)}/asset/${path}${revision ? '?revision='+encodeURIComponent(revision):''}" alt="${escape(alt)}" loading="lazy">`;
      }
      return `<span class="image-unavailable">[配图未加载：${escape(alt)}]</span>`;
    };
    const linkOpen=md.renderer.rules.link_open || ((tokens,index,opts,env,self) => self.renderToken(tokens,index,opts));
    md.renderer.rules.link_open = (tokens,index,opts,env,self) => {
      const token=tokens[index], href=token.attrGet('href') || '';
      if (/^source:\d+$/.test(href) && lessonId) token.attrSet('href',`/api/lessons/${encodeURIComponent(lessonId)}/source/${href.slice(7)}`);
      else if (/^lesson:[a-zA-Z0-9_-]+$/.test(href)) token.attrSet('href',`/?lesson=${encodeURIComponent(href.slice(7))}`);
      else if (!/^(https?:\/\/|#)/i.test(href)) token.attrSet('href','#');
      token.attrSet('rel','noreferrer noopener');
      return linkOpen(tokens,index,opts,env,self);
    };
    return md.render(String(source || ''));
  }};
  document.addEventListener('click', event => {
    const button=event.target.closest('[data-remote-image]');
    if (!button || !safeExternal(button.dataset.remoteImage)) return;
    const image=new Image(); image.alt=button.dataset.alt; image.referrerPolicy='no-referrer';
    image.loading='lazy'; image.src=button.dataset.remoteImage;
    image.onerror=() => { image.replaceWith(document.createTextNode('图片暂不可用，正文仍可阅读。')); };
    button.replaceWith(image);
  });
})();
