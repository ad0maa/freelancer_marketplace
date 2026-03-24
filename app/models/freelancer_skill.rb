class FreelancerSkill < ApplicationRecord
  belongs_to :freelancer
  belongs_to :skill

  validates :freelancer_id, uniqueness: { scope: :skill_id }
end
