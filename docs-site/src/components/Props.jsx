import Link from '@docusaurus/Link';

function renderType(type, href) {
  if (!type) return null;
  if (typeof type !== 'string') return type;

  const [first, ...rest] = type.split(' | ');
  return (
    <>
      {href ? <Link to={href}>{first}</Link> : first}
      {rest.length > 0 && <> | {rest.join(' | ')}</>}
    </>
  );
}

export function Props({ children }) {
  return <dl className="props">{children}</dl>;
}

export function Prop({ name, type, href, iterator, children }) {
  const typeNode = iterator ? (
    <>
      <Link to="/paginated-iterator">PaginatedIterator</Link>
      {'['}
      {typeof type === 'string'
        ? (href ? <Link to={href}>{type}</Link> : type)
        : type}
      {']'}
    </>
  ) : renderType(type, href);

  return (
    <>
      <dt>
        <code>{name}</code>
        {typeNode && (
          <span className="props__type">
            {iterator ? ' -> ' : ' : '}
            {typeNode}
          </span>
        )}
      </dt>
      <dd>{children}</dd>
    </>
  );
}