// @ts-check

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'Facebook GraphQL Scraper',
  tagline: 'Scrape Facebook GraphQL data without an API key',
  favicon: 'img/favicon.ico',

  url: 'https://XD-MHLOO.github.io',
  baseUrl: '/facebook-gql-scraper/',

  organizationName: 'XD-MHLOO',
  projectName: 'facebook-gql-scraper',

  onBrokenLinks: 'throw',

  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          routeBasePath: '/',
          sidebarPath: './sidebars.js',
          include: ['**/*.{md,mdx}'],
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      
      colorMode: {
        defaultMode: 'dark',          
        disableSwitch: false,          
        respectPrefersColorScheme: true, 
      },

      navbar: {
        title: 'facebook-gql-scraper',

        items: [
          { type: 'docSidebar', sidebarId: 'docs', position: 'left', label: 'Docs' },
          {
            href: 'https://github.com/XD-MHLOO/facebook-gql-scraper',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
    }),

    themes: [
      [
        require.resolve('@easyops-cn/docusaurus-search-local'),
        {
          hashed: true,
          language: ['en'],
          indexDocs: true,
          indexBlog: false,
          docsRouteBasePath: '/',
        },
      ],
    ],

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },
};

export default config;