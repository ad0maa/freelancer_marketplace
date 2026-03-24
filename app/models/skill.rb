class Skill < ApplicationRecord
  validates :name, presence: true, uniqueness: true
end
