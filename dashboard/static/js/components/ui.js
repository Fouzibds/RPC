/**
 * Bibliothèque de composants du laboratoire — point d'entrée unique.
 *
 *     import { Card, Button, Stat, Table } from '../components/ui.js';
 *
 * Chaque composant est une fonction qui renvoie un nœud DOM ; quand c'est utile, le nœud
 * porte une petite API de mise à jour (`setValue`, `update`, `setRows`…). La page `#/kit`
 * montre chaque composant dans toutes ses variantes.
 */

export { PageHeader, Card, Section, Grid, Col, Stack, Divider } from './ui/layout.js';
export { Button, IconButton, ButtonGroup, CopyButton } from './ui/buttons.js';
export { Input, Textarea, NumberInput, Toggle, Checkbox, Field } from './ui/forms.js';
export { Select } from './ui/select.js';
export { Slider } from './ui/slider.js';
export { Segmented, Tabs } from './ui/segmented.js';
export { Badge, Chip, ProtocolChip, StatusDot, Kbd } from './ui/badges.js';
export { CountUp, Stat, KeyValue, Stepper } from './ui/data.js';
export { Table } from './ui/table.js';
export { EmptyState, Skeleton, Progress, Callout } from './ui/feedback.js';
export { Tooltip } from './ui/tooltip.js';
export { openMenu, openPopover } from './ui/floating.js';
