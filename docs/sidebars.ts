import type { SidebarsConfig } from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  tutorialSidebar: [
    {
      type: 'category',
      label: 'Getting Started',
      collapsed: false,
      items: [
        'getting_started/overview',
        'getting_started/quickstart',
        'getting_started/installation',
      ],
    },
    {
      type: 'category',
      label: 'Tutorials',
      collapsed: false,
      items: [
        'tutorials/live_updates',
        'tutorials/manage_flow_dynamically',
      ],
    },
    {
      type: 'category',
      label: 'CocoIndex Core',
      collapsed: false,
      items: [
        'core/basics',
        'core/data_types',
        'core/flow_def',
        'core/settings',
        'core/flow_methods',
        'core/cli',
      ],
    },
    {
      type: 'category',
      label: 'Built-in Operations',
      collapsed: false,
      items: [
        'ops/sources',
        'ops/functions',
        'ops/targets',
      ],
    },
    {
      type: 'category',
      label: 'Custom Operations',
      collapsed: false,
      items: [
        'custom_ops/custom_functions',
        'custom_ops/custom_targets',
      ],
    },
    {
      type: 'category',
      label: 'AI Support',
      collapsed: false,
      items: [
        'ai/llm',
      ],
    },
    {
      type: 'doc',
      id: 'query',
      label: 'Query Support',
    },
    {
      type: 'category',
      label: 'About',
      collapsed: false,
      items: [
        'about/community',
        'about/contributing',
      ],
    },
  ],
};

export default sidebars;
