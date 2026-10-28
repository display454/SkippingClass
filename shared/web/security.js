/* Only same-origin writes receive this instance's request token. */
(() => {
  const original = window.fetch.bind(window);
  let token;
  const ready = () => token || (token = original('/api/security', {cache:'no-store'})
    .then(r => { if (!r.ok) throw new Error('无法建立本机会话'); return r.json(); })
    .then(data => data.token));
  window.fetch = async (input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (url.origin === location.origin && !['GET','HEAD','OPTIONS'].includes(method)) {
      const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
      headers.set('X-CSRF-Token', await ready());
      options = {...options, headers};
    }
    return original(input, options);
  };
})();
