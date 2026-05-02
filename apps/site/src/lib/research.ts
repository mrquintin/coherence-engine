import ContradictionDirection, {
  frontmatter as contradictionDirection,
} from '../content/research/contradiction_direction.mdx';
import CosineParadox, { frontmatter as cosineParadox } from '../content/research/cosine_paradox.mdx';
import DecisionPolicyV1, {
  frontmatter as decisionPolicyV1,
} from '../content/research/decision_policy_v1.mdx';
import ReverseMarxism, {
  frontmatter as reverseMarxism,
} from '../content/research/reverse_marxism.mdx';

export interface ResearchFrontmatter {
  title: string;
  summary: string;
  authors: string[];
  published: string;
  updated?: string;
  status: 'hypothesis' | 'preliminary' | 'replicated' | 'archived';
  tags: string[];
  citation?: string;
  draft?: boolean;
}

export const researchPapers = [
  {
    slug: 'contradiction_direction',
    data: contradictionDirection as ResearchFrontmatter,
    Content: ContradictionDirection,
  },
  {
    slug: 'cosine_paradox',
    data: cosineParadox as ResearchFrontmatter,
    Content: CosineParadox,
  },
  {
    slug: 'decision_policy_v1',
    data: decisionPolicyV1 as ResearchFrontmatter,
    Content: DecisionPolicyV1,
  },
  {
    slug: 'reverse_marxism',
    data: reverseMarxism as ResearchFrontmatter,
    Content: ReverseMarxism,
  },
] as const;

export function getPublishedResearchPapers() {
  return researchPapers
    .filter((paper) => !('draft' in paper.data && paper.data.draft))
    .toSorted((a, b) => Date.parse(b.data.published) - Date.parse(a.data.published));
}

export function getResearchPaper(slug: string) {
  return researchPapers.find((paper) => paper.slug === slug);
}
