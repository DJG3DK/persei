// pm2 for the newsletter signup. Its own file, not the agent's
// ecosystem.config.js, because this belongs to perseiagent.ai rather than to the
// product -- the same line site/ is on the other side of everywhere else.
//
//   pm2 start site/server/ecosystem.config.cjs && pm2 save
//
// .cjs, not .js: site/package.json declares "type": "module", which would
// make pm2 load this as ESM and die on module.exports.
module.exports = {
  apps: [
    {
      name: 'tektonix-newsletter',
      script: '.venv/bin/uvicorn',
      args: 'newsletter:app --host 127.0.0.1 --port 8300',
      cwd: __dirname,
      interpreter: 'none',
      autorestart: true,
      // It holds one small pool and answers a handful of requests a week.
      max_memory_restart: '150M',
      env: {
        // Falls back to the agent's database, which is what the box already
        // backs up (docs/backup.md). Its table is its own.
        SITE_URL: 'https://perseiagent.ai',
        NEWSLETTER_PORT: '8300',
      },
    },
  ],
};
