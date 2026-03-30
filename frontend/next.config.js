/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://localhost:5000/api/:path*',
      },
    ];
  },
  // Increase proxy timeout for slow endpoints like Gemini AI insights
  experimental: {
    proxyTimeout: 120_000,
  },
};

module.exports = nextConfig;
