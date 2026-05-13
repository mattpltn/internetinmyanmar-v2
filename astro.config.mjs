// @ts-check
import { defineConfig } from 'astro/config'
import mdx from '@astrojs/mdx'
import sitemap from '@astrojs/sitemap'
import tailwindcss from '@tailwindcss/vite'
import keystatic from '@keystatic/astro'
import cloudflare from '@astrojs/cloudflare'

export default defineConfig({
  site: 'https://www.internetinmyanmar.com',
  trailingSlash: 'always',
  output: 'static',
  adapter: cloudflare(),

  integrations: [
    mdx(),
    sitemap(),
    keystatic(),
  ],

  vite: {
    plugins: [tailwindcss()],
  },
})
