create or replace function public.sol_claim_due_outbox(p_limit integer, p_now timestamptz)
returns table(id uuid, idempotency_key text, recipient text, subject text, body_text text, attempt_count integer, expires_at timestamptz)
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.sol_outbox as stale
  set status = 'pending', claimed_at = null
  where stale.status = 'sending'
    and stale.claimed_at is not null
    and stale.claimed_at <= p_now - interval '5 minutes'
    and stale.expires_at > p_now
    and stale.attempt_count < 4;

  update public.sol_outbox as expired
  set status = 'expired', last_error = 'expired while claimed'
  where expired.status = 'sending'
    and expired.expires_at <= p_now;

  return query
  update public.sol_outbox as claimed
  set status = 'sending', attempt_count = claimed.attempt_count + 1, claimed_at = p_now
  where claimed.id in (
    select candidate.id
    from public.sol_outbox as candidate
    where candidate.status = 'pending'
      and candidate.attempt_count < 4
      and candidate.next_attempt_at <= p_now
      and candidate.expires_at > p_now
    order by candidate.next_attempt_at, candidate.created_at
    for update skip locked
    limit greatest(1, least(p_limit, 50))
  )
  returning claimed.id, claimed.idempotency_key, claimed.recipient, claimed.subject,
    claimed.body_text, claimed.attempt_count, claimed.expires_at;
end;
$$;

revoke all on function public.sol_claim_due_outbox(integer, timestamptz) from public, anon, authenticated;
grant execute on function public.sol_claim_due_outbox(integer, timestamptz) to service_role;
