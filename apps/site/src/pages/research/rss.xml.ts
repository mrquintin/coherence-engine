import rss from '@astrojs/rss';
import type { APIContext } from 'astro';
import { getPublishedResearchPapers } from '~/lib/research';

export async function GET(context: APIContext) {
  const papers = getPublishedResearchPapers();
  return rss({
    title: 'Coherence Engine — Research',
    description:
      'Working papers from the Coherence Engine research program. Predictive validity unproven; all claims exploratory.',
    site: context.site!,
    items: papers.map((paper) => ({
      title: paper.data.title,
      pubDate: new Date(paper.data.published),
      description: paper.data.summary,
      link: `/research/${paper.slug}/`,
      categories: paper.data.tags,
    })),
    customData: '<language>en-us</language>',
  });
}
