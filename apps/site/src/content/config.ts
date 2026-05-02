import { defineCollection, z } from 'astro:content';

const research = defineCollection({
  type: 'content',
  schema: z.object({
    title: z.string(),
    summary: z.string(),
    authors: z.array(z.string()).default(['Coherence Engine Research']),
    published: z.string(),
    updated: z.string().optional(),
    status: z.enum(['hypothesis', 'preliminary', 'replicated', 'archived']).default('hypothesis'),
    tags: z.array(z.string()).default([]),
    citation: z.string().optional(),
    draft: z.boolean().default(false),
  }),
});

export const collections = { research };
