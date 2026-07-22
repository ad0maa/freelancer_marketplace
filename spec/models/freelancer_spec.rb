require 'rails_helper'

RSpec.describe Freelancer, type: :model do
  describe 'validations' do
    it 'is valid with a name, email and hourly_rate' do
      expect(build(:freelancer)).to be_valid
    end

    it 'is invalid without a name' do
      freelancer = build(:freelancer, name: nil)
      expect(freelancer).not_to be_valid
      expect(freelancer.errors[:name]).to include("can't be blank")
    end

    it 'is invalid without an email' do
      freelancer = build(:freelancer, email: nil)
      expect(freelancer).not_to be_valid
      expect(freelancer.errors[:email]).to include("can't be blank")
    end

    it 'is invalid with a duplicate email' do
      create(:freelancer, email: 'jane@example.com')
      duplicate = build(:freelancer, email: 'jane@example.com')
      expect(duplicate).not_to be_valid
      expect(duplicate.errors[:email]).to include('has already been taken')
    end

    it 'is invalid with a malformed email' do
      freelancer = build(:freelancer, email: 'notanemail')
      expect(freelancer).not_to be_valid
      expect(freelancer.errors[:email]).to include('is invalid')
    end

    it 'is invalid with a negative hourly rate' do
      freelancer = build(:freelancer, hourly_rate: -10)
      expect(freelancer).not_to be_valid
      expect(freelancer.errors[:hourly_rate]).to include('must be greater than or equal to 0')
    end

    it 'is valid with an hourly rate of zero' do
      expect(build(:freelancer, hourly_rate: 0)).to be_valid
    end
  end

  describe 'defaults' do
    it 'defaults availability to true' do
      freelancer = create(:freelancer)
      expect(freelancer.availability).to be true
    end

    it 'can be marked as unavailable' do
      freelancer = create(:freelancer, availability: false)
      expect(freelancer.availability).to be false
    end
  end

  describe 'associations' do
    it 'has many freelancer_skills' do
      freelancer = create(:freelancer)
      skill = create(:skill)
      create(:freelancer_skill, freelancer: freelancer, skill: skill)
      expect(freelancer.freelancer_skills.count).to eq(1)
    end

    it 'has many skills through freelancer_skills' do
      freelancer = create(:freelancer)
      skill = create(:skill)
      create(:freelancer_skill, freelancer: freelancer, skill: skill)
      expect(freelancer.skills).to include(skill)
    end

    it 'destroys associated freelancer_skills when deleted' do
      freelancer = create(:freelancer)
      create(:freelancer_skill, freelancer: freelancer, skill: create(:skill))
      expect { freelancer.destroy }.to change(FreelancerSkill, :count).by(-1)
    end

    it 'has many bookings' do
      freelancer = create(:freelancer)
      create(:booking, freelancer: freelancer)
      expect(freelancer.bookings.count).to eq(1)
    end

    it 'destroys associated bookings when deleted' do
      freelancer = create(:freelancer)
      create(:booking, freelancer: freelancer)
      expect { freelancer.destroy }.to change(Booking, :count).by(-1)
    end
  end
end
