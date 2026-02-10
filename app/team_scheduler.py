from datetime import datetime, time, date
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import Team, User, LeaveRequest, Attendance, TeamMember

GRACE_TIME = time(9, 30)


def auto_assign_leaders():
    # Only run logic after grace time
    if datetime.now().time() < GRACE_TIME:
        return

    db: Session = SessionLocal()
    try:
        teams = db.query(Team).all()

        for team in teams:
            perm_leader = team.permanent_leader
            
            # If no permanent leader defined, skip
            if not perm_leader:
                continue

            # 1. Check if Permanent Leader is Present Today
            is_perm_present = False
            
            # Check Leave
            on_leave = db.query(LeaveRequest).filter(
                LeaveRequest.employee_id == perm_leader.employee_id,
                LeaveRequest.status == "Approved",
                LeaveRequest.start_date <= date.today(),
                LeaveRequest.end_date >= date.today()
            ).first()

            # Check Attendance
            has_swiped = db.query(Attendance).filter(
                Attendance.employee_id == perm_leader.employee_id,
                Attendance.entry_time >= datetime.combine(date.today(), time(0, 0))
            ).first()

            if not on_leave and has_swiped:
                is_perm_present = True

            # --- LOGIC BRANCHING ---

            if is_perm_present:
                # Restoration: If current active leader is not the permanent one, swap back
                if team.leader_id != team.permanent_leader_id:
                    print(f"Original Leader {perm_leader.name} returned. Restoring command.")
                    team.leader_id = team.permanent_leader_id
                    db.commit()
            
            else:
                # Replacement: If permanent leader is absent, assign temporary
                print(f"Permanent Leader {perm_leader.name} is absent.")
                
                # Find members of THIS team specifically
                team_memberships = db.query(TeamMember).filter(TeamMember.team_id == team.id).all()
                member_ids = [tm.user_id for tm in team_memberships]
                
                # Fetch User objects who are capable of managing and are PRESENT
                candidates = db.query(User).filter(
                    User.id.in_(member_ids),
                    User.can_manage == True,
                    User.is_active == True,
                    User.id != perm_leader.id # Don't pick the absent boss
                ).all()

                present_candidates = []
                for cand in candidates:
                    is_here = db.query(Attendance).filter(
                        Attendance.employee_id == cand.employee_id,
                        Attendance.entry_time >= datetime.combine(date.today(), time(0, 0))
                    ).first()
                    if is_here:
                        present_candidates.append(cand)

                if present_candidates:
                    # Pick random or logic based (here: random for fairness/simplicity)
                    new_temp_leader = present_candidates[0]
                    if team.leader_id != new_temp_leader.id:
                        team.leader_id = new_temp_leader.id
                        print(f"Assigned temporary leader: {new_temp_leader.name}")
                        db.commit()

    except Exception as e:
        print(f"Scheduler Error: {e}")
    finally:
        db.close()