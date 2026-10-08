import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const bridge = process.env.BRIDGE_URL || 'http://127.0.0.1:8765';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: { '/ws': { target: bridge.replace(/^http/, 'ws'), ws: true }, '/health': bridge },
  },
});
