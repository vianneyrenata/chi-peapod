// CloudFront Function (viewer-request) that 403s announced AI crawlers and scraping tools BEFORE the edge cache.
// Needed because CloudFront does not forward User-Agent on the static behavior, so nginx's identical rule
// (deploy/nginx.conf, $hfes_blocked_bot) only protects /api/*; the CSV is served from the edge cache.
// Console: CloudFront > Functions > Create function (runtime 2.0), paste, Publish; then Distribution > Behaviors >
// edit the DEFAULT (*) behavior > Function associations > Viewer request = this function. Free tier covers 2M calls/month.
// Keep the list in sync with the map in nginx.conf. A scraper that claims to be Chrome still gets through.
function handler(event) {
  var h = event.request.headers['user-agent'];
  var ua = h ? h.value : '';
  if (/(GPTBot|ChatGPT-User|OAI-SearchBot|ClaudeBot|Claude-Web|anthropic-ai|Claude-SearchBot|Claude-User|Google-Extended|GoogleOther|CCBot|PerplexityBot|Perplexity-User|Bytespider|Amazonbot|Applebot-Extended|FacebookBot|meta-externalagent|meta-externalfetcher|cohere-ai|cohere-training-data-crawler|Diffbot|ImagesiftBot|Omgilibot|Omgili|Timpibot|YouBot|DuckAssistBot|MistralAI-User|AI2Bot|Ai2Bot-Dolma|PetalBot|SemrushBot|AhrefsBot|MJ12bot|DataForSeoBot|Scrapy|HTTrack)/i.test(ua)) {
    return { statusCode: 403, statusDescription: 'Forbidden' };
  }
  return event.request;
}
